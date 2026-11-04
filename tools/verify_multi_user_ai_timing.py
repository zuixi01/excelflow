"""Isolated, real-model concurrent login/upload timing check. Never writes live tasks."""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from io import BytesIO
from pathlib import Path
from threading import Barrier

import httpx
from openpyxl import Workbook

from excel_auditor.persistence import DatabaseRepository
from excel_auditor.workflow.ai import AIClient
from excel_auditor.workflow.field_profiles import FieldProfileService, ProfileEdit
from excel_auditor.workflow.repository import WorkflowStore


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'outputs' / 'concurrent-ai-20261104'
REAL_FILES = [ROOT / 'outputs' / 'formula-image-validation-20261027' / 'uploads' / name
              for name in ('玻璃柜门-美斯洛.xlsx', '灯具-花灯.xlsx', '洁具-安华.xlsx', '木门防火门-金迪.xlsx')]
REAL = os.environ.get('EXCELFLOW_TEST_MODE') == 'real'
USERS = len(REAL_FILES) if REAL else int(os.environ.get('EXCELFLOW_TEST_USERS', '6'))


def seconds(start: str | None, finish: str | None) -> float | None:
    if not start or not finish:
        return None
    return round((datetime.fromisoformat(finish) - datetime.fromisoformat(start)).total_seconds(), 3)


def distribution(values: list[float]) -> dict:
    if not values:
        return {'count': 0}
    values = sorted(values)
    return {'count': len(values), 'min': round(values[0], 3),
            'p50': round(statistics.median(values), 3),
            'p95': round(values[min(len(values) - 1, int((len(values) - 1) * .95 + .5))], 3),
            'max': round(values[-1], 3)}


def workbook(index: int) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = '供应商清单'
    sheet.append(['供方商品标题', '商家编码', '生产厂商', '含税报价', '可售数量', f'场景描述{index}'])
    for row in range(1, 21):
        sheet.append([f'测试灯具 {index}-{row}', f'QA{index:02d}{row:03d}', '测试厂牌', 15 + row, 3 + row, '室内照明'])
    content = BytesIO()
    book.save(content)
    book.close()
    return content.getvalue()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    if not 1 <= USERS <= 50:
        raise ValueError('EXCELFLOW_TEST_USERS must be 1..50')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    live_settings = ROOT / 'var' / 'workflow' / 'settings' / ('25bf8e1a2393f1108d37029b3df5593236c755742ec93465bbafa9b290bddcf6.json')
    if not live_settings.is_file():
        raise RuntimeError('当前 local 模型设置不存在，无法进行真实 AI 测试')
    started = datetime.now().astimezone().isoformat()
    report = {'started_at': started, 'isolated': True, 'users': USERS,
              'workbooks': 'existing actual Excel copies' if REAL else 'synthetic 20 rows x 6 columns', 'model': '',
              'results': [], 'summary': {}, 'completed': False}
    with tempfile.TemporaryDirectory(prefix='excelflow-concurrent-ai-', ignore_cleanup_errors=True) as temporary:
        data = Path(temporary)
        settings_dir = data / 'workflow' / 'settings'
        settings_dir.mkdir(parents=True)
        shutil.copy2(live_settings, settings_dir / live_settings.name)
        os.environ['EXCEL_AUDITOR_ALLOW_INSECURE_AI_HTTP'] = '1'
        model = AIClient(data / 'workflow', 'local')
        public = model.settings()
        if not public['configured'] or not model.settings(private=True)['api_key']:
            raise RuntimeError('隔离副本无法读取当前模型配置')
        report['model'] = public['model']
        live_profile = httpx.get('http://127.0.0.1:8000/api/v1/field-profile', timeout=10, trust_env=False)
        live_profile.raise_for_status()
        published = live_profile.json()['payload'].get('published')
        if not published:
            raise RuntimeError('当前字段模板未发布，无法模拟真实上传')
        store = WorkflowStore(data / 'workflow')
        try:
            profiles = FieldProfileService(store)
            initial = profiles.get('local')
            saved = profiles.save('local', ProfileEdit(base_revision=initial['revision'], profile=published))
            profiles.publish('local', saved['revision'])
        finally:
            store.engine.dispose()
        password = secrets.token_urlsafe(24)
        admin_password = secrets.token_urlsafe(24)
        auth_db = DatabaseRepository(f"sqlite:///{(data / 'auth.sqlite3').as_posix()}", create_schema=True)
        auth_db.ensure_local_admin(username='load_admin', password=admin_password)
        usernames = [f'load_user_{index+1}' for index in range(USERS)]
        for username in usernames:
            auth_db.create_user(tenant_id='local', username=username, password=password, role='operator')
        auth_db.engine.dispose()
        port = free_port()
        base = f'http://127.0.0.1:{port}/api/v1'
        env = {**os.environ, 'EXCEL_AUDITOR_DATA': str(data), 'EXCEL_AUDITOR_ENV': 'development',
               'EXCEL_AUDITOR_REQUIRE_AUTH': '1', 'EXCEL_AUDITOR_ADMIN_USERNAME': 'load_admin',
               'EXCEL_AUDITOR_ADMIN_PASSWORD': admin_password,
               'EXCEL_AUDITOR_RECOVERY_INTERVAL': '1', 'EXCEL_AUDITOR_WORKFLOW_MAX_RUNNING': '2',
               'PYTHONPATH': str(ROOT / 'src'), 'PYTHONIOENCODING': 'utf-8'}
        for key in ('DATABASE_URL', 'REDIS_URL', 'EXCEL_AUDITOR_API_TOKEN', 'EXCEL_AUDITOR_API_TOKENS_JSON',
                    'EXCEL_AUDITOR_CONNECTIONS', 'EXCEL_AUDITOR_SECRET_DIR'):
            env.pop(key, None)
        log = (data / 'server.log').open('w', encoding='utf-8')
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'excel_auditor.api:app',
                                    '--app-dir', str(ROOT / 'src'), '--host', '127.0.0.1', '--port', str(port)],
                                   cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        worker_process = None
        worker_log = None
        clients: list[httpx.Client] = []
        try:
            for _ in range(90):
                if process.poll() is not None:
                    raise RuntimeError('隔离服务启动失败')
                try:
                    if httpx.get(base.replace('/api/v1', '/health/ready'), timeout=2, trust_env=False).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.5)
            else:
                raise TimeoutError('隔离服务 45 秒内未启动')
            worker_log = (data / 'worker.log').open('w', encoding='utf-8')
            worker_process = subprocess.Popen([sys.executable, '-m', 'excel_auditor.workflow.execution'],
                cwd=ROOT, env=env, stdout=worker_log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            for _ in range(30):
                heartbeat = data / 'workflow' / 'recovery.heartbeat'
                if worker_process.poll() is not None:
                    raise RuntimeError('隔离 Worker 启动失败')
                if heartbeat.is_file():
                    break
                time.sleep(.2)
            else:
                raise TimeoutError('隔离 Worker 6 秒内未启动')
            logins = []
            for username in usernames:
                client = httpx.Client(base_url=base, timeout=httpx.Timeout(30, connect=5), trust_env=False)
                t0 = time.perf_counter()
                login = client.post('/auth/login', json={'username': username, 'password': password})
                login.raise_for_status()
                csrf = client.get('/auth/csrf')
                csrf.raise_for_status()
                clients.append(client)
                logins.append({'user': username, 'user_id': login.json()['user_id'],
                               'seconds': round(time.perf_counter() - t0, 3), 'csrf': csrf.json()['csrf_token']})
            if len({item['user_id'] for item in logins}) != USERS:
                raise RuntimeError('测试账号未获得独立身份')
            barrier = Barrier(USERS)

            def upload(index: int) -> dict:
                username = usernames[index]
                client = clients[index]
                try:
                    barrier.wait(timeout=15)
                    start = time.perf_counter()
                    source = REAL_FILES[index] if REAL else None
                    content = source.read_bytes() if source else workbook(index+1)
                    upload_elapsed = 0.0
                    retry_wait = 0.0
                    statuses = []
                    for attempt in range(3):
                        attempt_start = time.perf_counter()
                        response = client.post('/standardization-tasks',
                            data={'name': f'并发AI验收-{index+1}'},
                            files={'excel_file': (source.name if source else f'并发样本-{index+1}.xlsx', content,
                                                  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')},
                            headers={'X-CSRF-Token': logins[index]['csrf']})
                        upload_elapsed += time.perf_counter() - attempt_start
                        statuses.append(response.status_code)
                        if response.status_code != 429 or attempt == 2:
                            break
                        pause = max(1, min(10, int(response.headers.get('Retry-After', '10'))))
                        time.sleep(pause)
                        retry_wait += pause
                    upload_seconds = round(upload_elapsed, 3)
                    if response.status_code != 202:
                        failure = response.json()
                        return {'user': username, 'upload_status': response.status_code,
                                'upload_seconds': upload_seconds, 'error': failure.get('code', 'HTTP_ERROR'),
                                'detail': str(failure.get('detail', ''))[:200], 'attempt_statuses': statuses,
                                'retry_wait_seconds': retry_wait}
                    accepted_elapsed = round(time.perf_counter() - start, 3)
                    task_id = response.json()['id']
                    deadline = time.monotonic() + 360
                    task = None
                    while time.monotonic() < deadline:
                        result = client.get(f'/workflow-tasks/{task_id}')
                        result.raise_for_status()
                        task = result.json()
                        if task['status'] in {'mapping', 'failed', 'cancelled'}:
                            break
                        time.sleep(.5)
                    if task is None or task['status'] not in {'mapping', 'failed', 'cancelled'}:
                        return {'user': username, 'task_id': task_id, 'upload_status': 202,
                                'upload_seconds': upload_seconds, 'status': 'timeout'}
                    progress = task['payload'].get('processing_progress') or {}
                    sheets = task['payload'].get('sheets') or []
                    return {'user': username, 'task_id': task_id, 'upload_status': 202,
                            'source_file': source.name if source else f'并发样本-{index+1}.xlsx',
                            'source_mib': round(len(content) / 1024**2, 2),
                            'upload_seconds': upload_seconds,
                            'submit_to_accept_seconds': accepted_elapsed,
                            'attempt_statuses': statuses, 'retry_wait_seconds': retry_wait,
                            'end_to_end_seconds': round(time.perf_counter() - start, 3),
                            'status': task['status'], 'stage': task['payload'].get('stage'),
                            'queue_seconds': seconds(task['created_at'], progress.get('started_at')),
                            'ai_stage_seconds': seconds(progress.get('ai_started_at'), progress.get('ai_finished_at')),
                            'processing_seconds': seconds(progress.get('started_at'), progress.get('finished_at')),
                            'ai_batches': progress.get('total_batches', 0),
                            'failed_ai_batches': progress.get('failed_batches', 0),
                            'model_statuses': [sheet.get('mapping_summary', {}).get('model_status') for sheet in sheets],
                            'model_error_codes': [error.get('code') for sheet in sheets for error in sheet.get('model_errors', [])],
                            'error': task['payload'].get('error', '')[:200]}
                except Exception as exc:
                    return {'user': username, 'status': 'exception', 'error': type(exc).__name__ + ': ' + str(exc)[:200]}

            with ThreadPoolExecutor(max_workers=USERS) as pool:
                report['results'] = list(pool.map(upload, range(USERS)))
            report['summary'] = {
                'logins_succeeded': len(logins), 'uploads_accepted': sum(r.get('upload_status') == 202 for r in report['results']),
                'first_attempt_accepted': sum(r.get('attempt_statuses', [None])[0] == 202 for r in report['results']),
                'first_attempt_throttled': sum(r.get('attempt_statuses', [None])[0] == 429 for r in report['results']),
                'tasks_mapped': sum(r.get('status') == 'mapping' for r in report['results']),
                'ai_completed': sum('completed' in r.get('model_statuses', []) for r in report['results']),
                'upload_seconds': distribution([r['upload_seconds'] for r in report['results'] if 'upload_seconds' in r]),
                'queue_seconds': distribution([r['queue_seconds'] for r in report['results'] if r.get('queue_seconds') is not None]),
                'submit_to_accept_seconds': distribution([r['submit_to_accept_seconds'] for r in report['results'] if 'submit_to_accept_seconds' in r]),
                'ai_stage_seconds': distribution([r['ai_stage_seconds'] for r in report['results'] if r.get('ai_stage_seconds') is not None]),
                'end_to_end_seconds': distribution([r['end_to_end_seconds'] for r in report['results'] if 'end_to_end_seconds' in r]),
            }
            report['completed'] = True
        finally:
            for client in clients:
                client.close()
            if worker_process is not None:
                worker_process.terminate()
                try:
                    worker_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    worker_process.kill()
                    worker_process.wait(timeout=5)
            if worker_log is not None:
                worker_log.close()
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            log.close()
            report['finished_at'] = datetime.now().astimezone().isoformat()
            (OUTPUT / ('real-files-parallel-report.json' if REAL else f'parallel-{USERS}-report.json')).write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            # The encrypted copy of the live model secret is never retained in outputs.
            (settings_dir / live_settings.name).unlink(missing_ok=True)
            time.sleep(1)
    print(json.dumps(report['summary'], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
