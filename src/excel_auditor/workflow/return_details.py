"""Immutable, seekable return differences; page without loading whole workbooks."""
import json
from openpyxl.utils import get_column_letter

from .models import WorkflowError
from .repository import identifier


def identity(row, values):
    source = row.get('source', {})
    return {'id': row.get('id', ''), 'name': values.get('product_name') or values.get('name', ''),
            'sku': values.get('sku', ''), 'brand': values.get('brand', ''),
            'source': {k: source.get(k) for k in ('filename', 'original_sheet', 'sheet', 'row', 'kind')}}


def write_details(directory, old_rows, *, proposed=None, changes=None, additions=None, missing=(),
                  locations=None, columns=None, original_depth=None):
    original = old_rows if isinstance(old_rows, dict) else {r['id']: r for r in old_rows}
    old_locations = {rid: {'sheet': '维护表', 'row': index + original_depth + 1}
                     for index, rid in enumerate(original)} if original_depth is not None else {}
    def position(rid, keys=()):
        location = (locations or {}).get(rid)
        cells = {}
        if location:
            for key in keys:
                column = (columns or {}).get(key)
                if column:
                    letter = get_column_letter(column['column'])
                    cells[key] = {**location, **column, 'letter': letter, 'address': f"{letter}{location['row']}"}
        return {'location': location, 'original_location': old_locations.get(rid), 'cells': cells}
    token = identifier('diff')
    offsets = {'changed': [], 'added': [], 'missing': []}
    with (directory / (token + '.jsonl')).open('wb') as handle:
        def append(kind, item):
            offsets[kind].append(handle.tell())
            handle.write((json.dumps(item, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8'))
        if proposed is not None:
            for row in proposed:
                values = row['values_json']
                old = original.get(row['id'])
                if old is None:
                    append('added', {**identity(row, values), **position(row['id'], values), 'values': values})
                else:
                    before = old['values_json']
                    delta = {k: v for k, v in values.items() if v != before.get(k, '')}
                    if delta:
                        append('changed', {**identity(old, values), **position(row['id'], delta), 'before': {k: before.get(k, '') for k in delta}, 'after': delta})
        else:
            for rid, delta in (changes or {}).items():
                old = original[rid]; before = old['values_json']
                append('changed', {**identity(old, {**before, **delta}), 'before': {k: before.get(k, '') for k in delta}, 'after': delta})
            for values in additions or []:
                append('added', {**identity({}, values), 'values': values})
        for rid in missing:
            row = original[rid]
            append('missing', {**identity(row, row['values_json']), **position(rid), 'values': row['values_json']})
    (directory / (token + '.index.json')).write_text(json.dumps(offsets), encoding='utf-8')
    return {'token': token, 'counts': {kind: len(values) for kind, values in offsets.items()}}


def field_changes(old_config, new_config, columns):
    """User-facing column differences with real Excel letters, not internal keys."""
    from .return_import import field_label, field_kind
    old = {f.rule.name: f for f in old_config.fields}
    new = {f.rule.name: f for f in new_config.fields}
    original_positions = {fid: i+2 for i, fid in enumerate(old)}
    existing_order = [fid for fid in old if fid in columns]
    returned_order = [fid for fid in columns if fid in old]
    before_order = {fid: i for i, fid in enumerate(existing_order)}
    after_order = {fid: i for i, fid in enumerate(returned_order)}
    labels = {'normal': '普通字段', 'attribute': '商品属性', 'specification': '商品规格'}
    result = []
    def add(kind, fid, before, after):
        source = columns.get(fid)
        result.append({'kind': kind, 'field': field_label(new.get(fid) or old[fid]),
                       'before': before, 'after': after,
                       'location': f"{source['sheet']} · {get_column_letter(source['column'])} 列" if source else '回传中已无此列',
                       'original_column': get_column_letter(original_positions[fid]) if fid in old else ''})
    for fid, column in columns.items():
        if fid not in old:
            add('新增列', fid, '无', field_label(new[fid]))
            continue
        if field_label(old[fid]) != field_label(new[fid]):
            add('改名', fid, field_label(old[fid]), field_label(new[fid]))
        if field_kind(old[fid]) != field_kind(new[fid]):
            add('调整归属', fid, labels[field_kind(old[fid])], labels[field_kind(new[fid])])
        if before_order[fid] != after_order[fid]:
            add('移动列', fid, get_column_letter(original_positions[fid])+' 列', get_column_letter(column['column'])+' 列')
    for fid in old:
        if fid not in columns:
            add('文件缺列' if fid in new else '删除列', fid, field_label(old[fid]), '保留原字段和原值' if fid in new else '确认后删除字段和数据')
    return result


def page_details(directory, manifest, kind, offset=0, limit=10):
    import re
    if kind not in {'changed', 'added', 'missing'}:
        raise WorkflowError('不支持的差异类型。', 400)
    token = manifest.get('token', '')
    if not re.fullmatch('diff_[a-f0-9]{32}', token):
        raise WorkflowError('差异预览不存在，请重新回传。', 404)
    offset, limit = max(0, offset), max(1, min(50, limit))
    indexes = json.loads((directory / (token + '.index.json')).read_text('utf-8'))[kind]
    items = []
    with (directory / (token + '.jsonl')).open('rb') as handle:
        for position in indexes[offset:offset + limit]:
            handle.seek(position)
            items.append(json.loads(handle.readline()))
    return {'items': items, 'total': len(indexes), 'offset': offset, 'limit': limit}
