"""Local-only UI acceptance server using the previously isolated test copy."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
report = json.loads((ROOT / 'outputs/production-hardening-20261103/actual-11/验收结果.json').read_text('utf-8'))
data_root = Path(report['runtime']).resolve()
if not data_root.is_relative_to((ROOT / 'outputs').resolve()):
    raise RuntimeError('acceptance data must remain under outputs')
os.environ.update(EXCEL_AUDITOR_DATA=str(data_root), EXCEL_AUDITOR_ENV='development', EXCEL_AUDITOR_REQUIRE_AUTH='1',
    EXCEL_AUDITOR_ADMIN_USERNAME='acceptance', EXCEL_AUDITOR_ADMIN_PASSWORD='isolated-acceptance-password',
    EXCEL_AUDITOR_RECOVERY_INTERVAL='2', DATABASE_URL='', REDIS_URL='', S3_BUCKET='',
    EXCEL_AUDITOR_API_TOKEN='', EXCEL_AUDITOR_API_TOKENS_JSON='')

if __name__ == '__main__':
    import uvicorn
    uvicorn.run('excel_auditor.api:app', host='127.0.0.1', port=8016)
