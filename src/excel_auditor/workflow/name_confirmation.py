"""Import an explicit product-name confirmation list, never a replacement table."""
from pathlib import Path
import tempfile

from .excel import MAX_ROWS, cell_text, safe_book
from .models import EditRequest, WorkflowConfig, WorkflowError


def read_confirmations(content: bytes):
    with tempfile.TemporaryDirectory(prefix='name-confirmation-') as directory:
        path = Path(directory) / 'confirmation.xlsx'
        from .uploads import persist
        persist(path, content)
        book = safe_book(path)
        try:
            matches = []
            for sheet in book.worksheets:
                if sheet.sheet_state != 'visible':
                    continue
                for row in sheet.iter_rows(max_row=min(sheet.max_row, 30)):
                    labels = [cell_text(cell.value).strip() for cell in row]
                    if '记录ID' in labels and '确认商品名称' in labels:
                        if labels.count('记录ID') != 1 or labels.count('确认商品名称') != 1:
                            raise WorkflowError('确认清单的“记录ID”和“确认商品名称”表头不能重复。')
                        matches.append((sheet, row[0].row, labels.index('记录ID') + 1, labels.index('确认商品名称') + 1))
            if len(matches) != 1:
                raise WorkflowError('请上传名称确认清单，且仅有一张表包含“记录ID”和“确认商品名称”两列；普通维护表请使用“回传维护表”。')
            sheet, header, id_column, name_column = matches[0]
            result, seen = [], set()
            for row in sheet.iter_rows(min_row=header + 1):
                if not any(cell.value is not None and cell_text(cell.value).strip() for cell in row):
                    continue
                identity, name = row[id_column - 1], row[name_column - 1]
                if identity.data_type in {'f', 'e'} or name.data_type in {'f', 'e'}:
                    raise WorkflowError(f'第 {identity.row} 行的记录ID或确认名称含公式/错误值，请粘贴为实际文本。')
                record_id, confirmed = cell_text(identity.value).strip(), cell_text(name.value).strip()
                if not record_id:
                    raise WorkflowError(f'第 {identity.row} 行缺少记录ID，请从原确认清单恢复，不能按行号猜测商品。')
                if record_id in seen:
                    raise WorkflowError(f'第 {identity.row} 行的记录ID重复，请核对后重新上传。')
                if len(confirmed) > 32767 or confirmed.startswith('='):
                    raise WorkflowError(f'第 {identity.row} 行的确认名称无效，请填写普通文本。')
                seen.add(record_id)
                result.append((record_id, confirmed, identity.row))
                if len(result) > MAX_ROWS:
                    raise WorkflowError('确认清单最多支持 20,000 条记录。')
            if not result:
                raise WorkflowError('确认清单没有商品记录。')
            return result
        finally:
            book.close()


def preview(service, task_id, tenant, content):
    current = service.get(task_id, tenant)
    task, rows = service.snapshot(task_id, tenant, current['revision'], include_rows=True)
    if task['status'] not in {'maintenance', 'ready', 'completed'}:
        raise WorkflowError('请先确认字段并生成维护数据，再回传名称确认清单。', 409)
    config = WorkflowConfig.model_validate(task['payload']['config'])
    field = next((f for f in config.fields if f.rule.name == 'product_name'), None)
    if field is None or not field.editable:
        raise WorkflowError('当前任务没有可编辑的商品名称字段。')
    by_id = {row['id']: row for row in rows}
    changes, samples = {}, []
    blank = unchanged = overwritten = 0
    for record_id, name, excel_row in read_confirmations(content):
        if record_id not in by_id:
            raise WorkflowError(f'第 {excel_row} 行的记录ID不属于当前任务，或该商品已删除；本次未修改数据。')
        row = by_id[record_id]
        if row['source'].get('kind') == 'context':
            raise WorkflowError(f'第 {excel_row} 行对应原表说明或合计行，不能当作商品补名称。')
        if not name:
            blank += 1
            continue
        old = row['values_json'].get('product_name', '')
        if old == name:
            unchanged += 1
            continue
        overwritten += bool(old)
        changes[record_id] = {'product_name': name}
        source = row['source']
        samples.append({'id': record_id, 'before': old, 'after': name,
                        'filename': source.get('filename', ''),
                        'sheet': source.get('original_sheet') or source.get('sheet', ''),
                        'row': source.get('row'), 'excel_row': excel_row})
    if len(changes) > 2000:
        raise WorkflowError('一次最多确认 2,000 条名称，请分批填写并上传；其他确认项留空即可。')
    with service.store.engine.begin() as con:
        latest = service.store.get(con, task_id, tenant, 'task')
        if latest['revision'] != task['revision']:
            raise WorkflowError('任务已更新，请重新上传确认清单以核对最新差异。', 409, 'REVISION_CONFLICT')
        obj = service.store.create(con, tenant, 'nameconfirmation', {
            'task_id': task_id, 'base_revision': task['revision'], 'changes': changes, 'samples': samples,
            'counts': {'changed': len(changes), 'blank': blank, 'unchanged': unchanged, 'overwritten': overwritten},
        }, 'ready')
    result = service.store.public(obj)
    result['payload'] = {k: v for k, v in result['payload'].items() if k != 'changes'}
    return result


def apply(service, task_id, tenant, actor, preview_id):
    with service.store.engine.connect() as con:
        obj = service.store.get(con, preview_id, tenant, 'nameconfirmation')
    if obj['payload']['task_id'] != task_id:
        raise WorkflowError('确认清单预览不属于当前任务。', 409)
    if not obj['payload']['changes']:
        raise WorkflowError('没有需要更新的名称；空白确认项不会清空原名称。')
    # A name confirmation names explicit records. Do not expand a merged source
    # group as ordinary maintenance edits do: omitted records must stay unchanged.
    return service.edit(task_id, tenant, actor, EditRequest(
        base_revision=obj['payload']['base_revision'], changes=obj['payload']['changes']),
        expand_shared=False, audit_action='confirm_product_names')
