"""Durable request/result jobs for expensive interactive workflow operations."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy import select

from .capacity import disk_check, serialize
from .models import EditRequest, FlexibleReturnRequest, MappingRequest, WorkflowError
from .repository import identifier, objects
from .uploads import digest, persist


ALLOWED = {'maintenance_download', 'return_inspect', 'return_preview', 'return_flexible', 'name_preview',
           'mapping_analyze', 'mapping_upgrade', 'mapping_confirm', 'edit', 'return_apply', 'name_apply', 'export_prepare'}


class OperationService:
    def __init__(self, store):
        self.store = store

    def create(self, task_id, tenant, actor, operation, args=None, content=None, key=None):
        if operation not in ALLOWED:
            raise WorkflowError('不支持的处理操作。')
        disk_check(self.store.root)
        args = args or {}
        fingerprint = hashlib.sha256(json.dumps([task_id, operation, args, digest(content) if content is not None else None],
            ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        object_id = 'op_' + hashlib.sha256(f'{tenant}\0{actor}\0{key}'.encode()).hexdigest()[:32] if key else identifier('op')
        with self.store.engine.begin() as con:
            serialize(con, self.store.engine)
            previous = con.execute(select(objects).where(objects.c.id == object_id)).mappings().first()
            if previous:
                if previous['tenant_id'] != tenant or previous['payload'].get('fingerprint') != fingerprint:
                    raise WorkflowError('重复请求标识对应不同内容。', 409)
                return self.status(dict(previous))
            task = self.store.get(con, task_id, tenant, 'task')
            obj = self.store.create(con, tenant, 'operation', {'task_id': task_id, 'actor': actor,
                'operation': operation, 'args': args, 'base_revision': task['revision'], 'fingerprint': fingerprint}, 'processing', object_id)
            if content is not None:
                persist(self.store.directory(object_id) / 'input.xlsx', content)
        return self.status(obj)

    @staticmethod
    def status(obj):
        return {'id': obj['id'], 'status': obj['status'], 'async_operation': True,
                'error': obj['payload'].get('error'), 'code': obj['payload'].get('error_code')}

    def get(self, task_id, tenant, operation_id):
        with self.store.engine.connect() as con:
            obj = self.store.get(con, operation_id, tenant, 'operation')
        if obj['payload']['task_id'] != task_id:
            raise WorkflowError('记录不存在或无权访问。', 404)
        return obj

    def run(self, operation_id, tenant):
        from .tasks import TaskService, write_json
        from .name_confirmation import preview as names_preview, apply as names_apply
        service = TaskService(self.store)
        with self.store.engine.connect() as con:
            obj = self.store.get(con, operation_id, tenant, 'operation')
            task = self.store.get(con, obj['payload']['task_id'], tenant, 'task')
        data = obj['payload']
        if task['revision'] != data['base_revision']:
            raise WorkflowError('排队期间数据已更新，请刷新后重新提交。', 409, 'REVISION_CONFLICT')
        disk_check(self.store.root)
        task_id, actor, args = task['id'], data['actor'], data['args']
        content = self.store.directory(operation_id) / 'input.xlsx'
        calls = {
            'return_inspect': lambda: service.inspect_return(task_id, tenant, content),
            'return_preview': lambda: service.preview_return(task_id, tenant, content),
            'return_flexible': lambda: service.preview_flexible_return(task_id, tenant, args['preview_id'], FlexibleReturnRequest.model_validate(args['body'])),
            'name_preview': lambda: names_preview(service, task_id, tenant, content),
            'mapping_analyze': lambda: service.analyze_mapping(task_id, tenant, actor, args['base_revision'], args.get('categories', {})),
            'mapping_upgrade': lambda: service.analyze_mapping(task_id, tenant, actor, args['base_revision'], upgrade_snapshot=True),
            'mapping_confirm': lambda: service.confirm_mapping(task_id, tenant, actor, MappingRequest.model_validate(args)),
            'edit': lambda: service.edit(task_id, tenant, actor, EditRequest.model_validate(args)),
            'return_apply': lambda: service.apply_return(task_id, tenant, actor, args['preview_id'], args.get('delete_missing', True),
                args.get('acknowledge_identity_warnings', False), args.get('confirm_structure', False), args.get('confirm_stale', False)),
            'name_apply': lambda: names_apply(service, task_id, tenant, actor, args['preview_id']),
            'export_prepare': lambda: service.create_export(task_id, tenant, args['base_revision'],
                idempotency=operation_id, output_template_id=args.get('output_template_id'))[0],
        }
        if data['operation'] == 'maintenance_download':
            from .tasks import maintenance_download_name
            generated = service.maintenance_file(task_id, tenant)
            try:
                stored_file = identifier('download') + '.xlsx'
                persist(self.store.directory(operation_id) / stored_file, generated)
                result = {'download': True, 'filename': maintenance_download_name(generated), 'stored_file': stored_file}
            finally:
                generated.unlink(missing_ok=True)
        else:
            result = calls[data['operation']]()
        # Attempt-specific results keep expired workers from overwriting a successor's file.
        result_name = identifier('result') + '.json'
        write_json(self.store.directory(operation_id) / result_name, result)
        with self.store.engine.begin() as con:
            obj['payload']['result_file'] = result_name
            obj['status'] = 'ready'
            self.store.save(con, obj)

    def result(self, obj):
        if obj['status'] != 'ready':
            raise WorkflowError('处理尚未完成，请稍后查询。', 409)
        return json.loads((self.store.directory(obj['id']) / obj['payload']['result_file']).read_text('utf-8'))
