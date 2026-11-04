"""Task-local, explicitly reviewed schema and record reconciliation for Excel returns.

Physical column/row numbers are locations, never business identities. Nothing in
this module writes task state; the service commits the reviewed snapshot once.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from copy import deepcopy

from openpyxl.utils.cell import range_boundaries
from pydantic import ValidationError

from ..models import ColumnRule
from ..product_workflow.fixed_template import normalize_header
from .excel import MAX_CELLS, MAX_ROWS, safe_book, transport_cell_text, cell_text
from .models import MaintenanceField, OutputColumn, WorkflowConfig, WorkflowError
from .repository import identifier
from .retained_columns import field_header_path
from .source_merges import vertical_spans
from .image_assets import image_text


def column_width(sheet, column):
    from openpyxl.utils import get_column_letter
    dimension = sheet.column_dimensions.get(get_column_letter(column))
    if dimension is None:
        dimension = next((d for d in sheet.column_dimensions.values() if d.min is not None and d.max is not None and d.min <= column <= d.max), None)
    return dimension.width if dimension is not None and dimension.width is not None else (sheet.sheet_format.defaultColWidth or 13)


def field_kind(field):
    if field.output_kind:
        return field.output_kind
    if '.attribute.' in field.rule.name or field.rule.title.startswith('动态字段/商品属性/'):
        return 'attribute'
    if '.specification.' in field.rule.name or field.rule.title.startswith('动态字段/商品规格/'):
        return 'specification'
    return 'normal'


def field_label(field):
    if field.header_path:
        return field.header_path[-1]
    return (field.source_column.header or f'未命名列 {field.source_column.column}') if field.source_column else field.rule.title.split('/')[-1]


def field_options(config):
    return [{'id': f.rule.name, 'title': f.rule.title, 'label': field_label(f), 'kind': field_kind(f),
             'required': f.rule.required, 'editable': f.editable} for f in config.fields]


def inspect_book(path, task):
    config = WorkflowConfig.model_validate(task['payload']['config'])
    book = safe_book(path, max_columns=10201, allow_images=True)
    try:
        if '_workflow_meta' not in book.sheetnames:
            raise WorkflowError('缺少任务版本信息。请从本任务下载维护表，在此文件上编辑；隐藏的任务信息工作表需要保留。')
        meta = {row[0]: row[1] for row in book['_workflow_meta'].iter_rows(values_only=True) if row[0]}
        for key in ('fields', 'titles', 'field_names'):
            if key + '_chunks' in meta:
                meta[key] = ''.join(str(meta.get(f'{key}_{i}', '')) for i in range(int(meta[key + '_chunks'])))
        if meta.get('task_id') != task['id']:
            raise WorkflowError('这份维护表属于其他任务，请进入对应任务回传，或下载当前任务的维护表。', 409, 'RETURN_TASK_MISMATCH')
        source_revision = meta.get('revision')
        if type(source_revision) is not int or not 0 <= source_revision <= task['revision']:
            raise WorkflowError('维护表版本信息无效或比当前任务更新，请刷新任务并核对文件。', 409, 'REVISION_CONFLICT')
        stale = source_revision < task['revision']
        if not stale and meta.get('template_hash') != task['payload']['template_hash']:
            raise WorkflowError('维护表的字段版本与当前任务不一致，请下载最新维护表后回传。', 409, 'RETURN_TEMPLATE_MISMATCH')
        depth = int(meta.get('header_depth', 1))
        if not 1 <= depth <= 50:
            raise WorkflowError('维护表头层数无效。')
        from .workbook_values import resolve_workbook
        resolve_workbook(book, path, active_sheets={s.title for s in book if s.sheet_state == 'visible' and not s.title.startswith('_')})
        try:
            names = json.loads(meta.get('field_names', '{}'))
        except (ValueError, TypeError):
            raise WorkflowError('维护表隐藏字段信息损坏，请重新下载维护表。')
        anchors, id_anchors = defaultdict(set), set()
        for name, definition in book.defined_names.items():
            if name not in names and name != '_ef_record_ids':
                continue
            try:
                destinations = list(definition.destinations)
                for title, bounds in destinations:
                    left, _top, right, _bottom = range_boundaries(bounds)
                    if left == right:
                        if name == '_ef_record_ids':
                            id_anchors.add((title, left))
                        else:
                            anchors[(title, left)].add(names[name])
            except (ValueError, TypeError, AttributeError):
                # Deleted column names become #REF!. Missing fields are reviewed below.
                continue
        fields = {f.rule.name: f for f in config.fields}
        full_paths, leaves = defaultdict(list), defaultdict(list)
        for f in config.fields:
            path_key = normalize_header('/'.join(field_header_path(f, layout=config.source_header_layout)))
            full_paths[path_key].append(f.rule.name)
            if normalize_header(f.rule.title) != path_key:
                full_paths[normalize_header(f.rule.title)].append(f.rule.name)
            leaves[normalize_header(field_label(f))].append(f.rule.name)
        columns, rows, merges, sheets, rectangles = [], [], [], [], []
        for si, sheet in enumerate(book.worksheets):
            if sheet.sheet_state != 'visible' or sheet.title.startswith('_'):
                continue
            header_values = {}
            for region in sheet.merged_cells.ranges:
                if region.min_row <= depth < region.max_row:
                    raise WorkflowError(f'{sheet.title} 的合并单元格跨越表头和数据区，请在回传前分开。')
                if region.max_row <= depth:
                    value = cell_text(sheet.cell(region.min_row, region.min_col).value)
                    for r in range(region.min_row, region.max_row + 1):
                        for c in range(region.min_col, region.max_col + 1):
                            header_values[r, c] = value
            sheet_columns, id_columns = [], []
            for c in range(1, sheet.max_column + 1):
                parts = []
                for r in range(1, depth + 1):
                    value = header_values.get((r, c), cell_text(sheet.cell(r, c).value))
                    if not value.strip():
                        value = ''
                    if value and (not parts or parts[-1] != value):
                        parts.append(value)
                if (sheet.title, c) in id_anchors or '_workflow_record_id' in parts:
                    id_columns.append(c)
                    continue
                # Blank inserted columns with values are still exposed for naming.
                if not parts and not any(sheet.cell(r, c).value is not None for r in range(depth + 1, sheet.max_row + 1)):
                    continue
                path = '/'.join(parts)
                leaf = parts[-1] if parts else ''
                candidates = sorted(anchors.get((sheet.title, c), set()) & fields.keys())
                method = '隐藏字段标识' if candidates else '表头名称'
                if not candidates:
                    candidates = full_paths.get(normalize_header(path), [])
                    if not candidates:
                        path_kind = 'specification' if '商品规格' in parts else 'attribute' if '商品属性' in parts else 'normal'
                        candidates = [fid for fid in leaves.get(normalize_header(leaf), []) if field_kind(fields[fid]) == path_kind]
                fid = candidates[0] if len(candidates) == 1 else ''
                if meta.get('transport_format') == 'table_v1' and fid:
                    # Legacy Table headers contain the whole display path in one
                    # cell; that path is not a requested rename of the leaf field.
                    leaf = field_label(fields[fid])
                    parts = field_header_path(fields[fid], layout=config.source_header_layout)
                kind = field_kind(fields[fid]) if fid else ('specification' if '商品规格' in parts else 'attribute' if '商品属性' in parts else 'normal')
                column = {'key': f's{si}:c{c}', 'sheet': sheet.title, 'column': c, 'path': path, 'header_path': parts,
                          'width': column_width(sheet, c),
                          'title': leaf, 'action': 'existing' if fid else 'new', 'field_id': fid,
                          'kind': kind, 'type': 'string', 'candidates': candidates, 'matched_by': method if fid else '请确认新增或对应已有字段'}
                columns.append(column)
                sheet_columns.append(column)
            if len(id_columns) > 1:
                raise WorkflowError(f'{sheet.title} 中有多个记录 ID 列，请只保留一个。')
            expanded, groups = {}, {}
            for region in sheet.merged_cells.ranges:
                if region.min_row <= depth:
                    continue
                if region.min_col != region.max_col:
                    covered=[next((col for col in sheet_columns if col['column']==c),None) for c in range(region.min_col,region.max_col+1)]
                    if config.source_retention_version<2 or not all(col and col['field_id'] in fields and fields[col['field_id']].source_column for col in covered):
                        raise WorkflowError(f'{sheet.title} 的 {region} 横跨多个商品字段，无法判断每列的值；横向合并只支持原表来源区。')
                    rectangles.append({'sheet':sheet.title,'start':region.min_row,'end':region.max_row,
                                       'keys':[c['key'] for c in covered],'group':f's{si}:{region}'})
                if any(c in id_columns for c in range(region.min_col,region.max_col+1)):
                    raise WorkflowError('记录 ID 不能合并，多条商品需要分别识别。')
                anchor = sheet.cell(region.min_row, region.min_col)
                if anchor.data_type == 'f':
                    raise WorkflowError('维护表包含公式，请粘贴为值。')
                group = f's{si}:{region}'
                for c in range(region.min_col,region.max_col+1):
                    key=f's{si}:c{c}'
                    for r in range(region.min_row, region.max_row + 1):
                        expanded[r,c] = transport_cell_text(anchor)
                        if region.max_row>region.min_row:groups[r,c] = group+f':{c}'
                    merges.append({'key': key, 'start': region.min_row, 'end': region.max_row, 'group': group+f':{c}'})
            for r in range(depth + 1, sheet.max_row + 1):
                if any(cell.data_type == 'f' for cell in sheet[r]):
                    raise WorkflowError(f'{sheet.title} 第 {r} 行包含公式，请粘贴为值。')
                values = {col['key']: expanded.get((r, col['column']), transport_cell_text(sheet.cell(r, col['column']))) for col in sheet_columns}
                rid = cell_text(sheet.cell(r, id_columns[0]).value).strip() if id_columns else ''
                if not rid and not any(values.values()):
                    continue
                rows.append({'key': f's{si}:r{r}', 'sheet': sheet.title, 'row': r, 'id': rid, 'values': values,
                             'groups': {col['key']: groups[r, col['column']] for col in sheet_columns if (r, col['column']) in groups}})
            sheets.append({'name': sheet.title, 'header_depth': depth, 'columns': len(sheet_columns), 'id_column': id_columns[0] if id_columns else None})
        if not rows or len(rows) > MAX_ROWS or len(columns) > 10200 or len(rows) * max(1, len(columns)) > MAX_CELLS:
            raise WorkflowError('回传需要有 1 至 20,000 条商品、最多 10,200 个业务列，且不超过 200 万单元格。')
        main = book[sheets[0]['name']]
        return {'columns': columns, 'rows': rows, 'merges': merges, 'sheets': sheets,
                'source_revision': source_revision, 'stale_revision': stale,
                'rectangles':rectangles,
                'header_heights': [main.row_dimensions[r].height for r in range(1, depth+1)]}
    finally:
        book.close()


def reconcile(task, old_rows, document, request):
    """Build a complete proposed revision, or return unresolved identity questions."""
    old_config = WorkflowConfig.model_validate(task['payload']['config'])
    existing = {f.rule.name: f for f in old_config.fields}
    by_key = {c['key']: c for c in document['columns']}
    choices = {c.key: c for c in request.columns}
    if len(choices) != len(request.columns) or set(choices) != set(by_key):
        raise WorkflowError('请为回传文件的每一列确认对应关系，不能重复或遗漏列。')
    old_by_id = {r['id']: r for r in old_rows}
    if set(request.missing_fields) - existing.keys():
        raise WorkflowError('缺少字段的处理包含未知字段。')
    if any(action != 'delete' for action in request.missing_fields.values()):
        raise WorkflowError('回传以文件内容为准，缺少的列将同步删除。如需保留，请在维护表中恢复该列后重新上传。')
    mapped, new_fields, summary = {}, [], []
    used = set()
    for choice in request.columns:
        column = by_key[choice.key]
        if choice.action == 'ignore':
            summary.append(f'忽略回传列：{column["path"] or choice.key}')
            continue
        title = choice.title
        if not title.strip() or any(ord(c) < 32 and c not in '\r\n\t' for c in title):
            raise WorkflowError('每个导入列都需要字段名，不能包含控制字符。')
        plain_title = re.sub(r'[\x00-\x1f]', ' ', title)
        if choice.action == 'existing':
            if choice.field_id not in existing or choice.field_id in used:
                raise WorkflowError('已有字段不能重复关联多个回传列；请检查列对应关系。')
            field = existing[choice.field_id].model_copy(deep=True)
            old_label = field_label(field)
            if old_label != title:
                prefix = field.rule.title[:-len(old_label)].rstrip('/') if field.rule.title.endswith(old_label) else field.rule.title.rsplit('/', 1)[0]
                field.rule.title = f'{prefix}/{plain_title}' if prefix else plain_title
                if field.source_column:
                    field.source_column.header = title
                summary.append(f'改名：{old_label} → {title}')
            if field_kind(field) != choice.kind:
                summary.append(f'调整归属：{title} → {dict(normal="普通字段", attribute="商品属性", specification="商品规格")[choice.kind]}')
                if choice.kind != 'normal':
                    field.source_column = None
                    field.rule.title = f'动态字段/{"商品属性" if choice.kind == "attribute" else "商品规格"}/{plain_title}'
                elif field.rule.title.startswith('动态字段/'):
                    field.rule.title = f'普通字段/{plain_title}'
            field.output_kind = choice.kind
        else:
            prefix = {'normal': '新增字段', 'attribute': '动态字段/商品属性', 'specification': '动态字段/商品规格'}[choice.kind]
            if choice.kind == 'normal' and len(column.get('header_path', [])) > 1:
                prefix = '/'.join(column['header_path'][:-1])
            prefix = re.sub(r'[\x00-\x1f]', ' ', prefix)
            field = MaintenanceField(rule=ColumnRule(name='task_extra.' + identifier('field'), title=f'{prefix}/{plain_title}', type=choice.type), output_kind=choice.kind)
            summary.append(f'新增字段：{title}（{dict(normal="普通字段", attribute="商品属性", specification="商品规格")[choice.kind]}）')
        path = [*column.get('header_path', [])[:-1], title]
        if choice.kind in {'attribute', 'specification'} and not ('动态字段' in path and ('商品属性' if choice.kind == 'attribute' else '商品规格') in path):
            path = ['动态字段', '商品属性' if choice.kind == 'attribute' else '商品规格', title]
        elif choice.kind == 'normal' and '动态字段' in path:
            path = ['普通字段', title]
        old_path = field_header_path(existing[choice.field_id], layout=old_config.source_header_layout) if choice.action == 'existing' else None
        if path != old_path:
            field.header_path = path
            if choice.action == 'existing' and title == field_label(existing[choice.field_id]):
                summary.append(f'表头分组变化：{title} → {" / ".join(path)}')
        field.maintenance_width = column['width']
        used.add(field.rule.name)
        new_fields.append(field)
        mapped[choice.key] = field.rule.name
    missing_fields = [f for f in old_config.fields if f.rule.name not in used]
    removed = set()
    protected = set(old_config.unique_key) | {old_config.category_field} | {v for c in old_config.cross_checks for v in (c.left, c.right)}
    for field in missing_fields:
        if field.rule.required or not field.editable or field.rule.name in protected:
            raise WorkflowError(f'{field_label(field)} 是必填、只读或业务规则引用字段，不能直接删除。请在维护表中恢复该列后重新上传；本次未保存任何修改。')
        removed.add(field.rule.name)
        summary.append(f'移除字段及其当前任务数据：{field_label(field)}')
    if not new_fields:
        raise WorkflowError('至少需要保留一个字段。')
    old_order = [f.rule.name for f in old_config.fields if f.rule.name in used]
    new_order = [f.rule.name for f in new_fields if f.rule.name in existing and f.rule.name in used]
    if new_order != old_order:
        summary.append('字段顺序按已确认的回传列顺序调整')
    config = old_config.model_copy(deep=True)
    config.fields = new_fields
    config.maintenance_header_heights = document.get('header_heights', [])
    config.import_rules = [r for r in config.import_rules if r.field not in removed]
    # Flexible schema editing is task-local and only available to standardization
    # tasks whose output consists of direct copies. Legacy SOP transforms stay intact.
    for output in config.outputs:
        for sheet in output.sheets:
            if sheet.group_by in removed or sheet.filter_field in removed or set(sheet.sku_fields) & removed:
                raise WorkflowError('删除字段被输出分组或筛选规则引用，请在维护表中恢复该列后重新上传。')
            if any(c.operation != 'copy' for c in sheet.columns):
                raise WorkflowError('此任务包含自定义输出转换，不能直接重建列结构；请保留原输出规则。')
            old_columns = {c.field: c for c in sheet.columns if c.operation == 'copy'}
            sheet.columns = [old_columns[f.rule.name].model_copy(update={'title': f.rule.title}) if f.rule.name in old_columns
                             else OutputColumn(title=f.rule.title, field=f.rule.name) for f in new_fields if not f.hidden]
    try:
        config = WorkflowConfig.model_validate(config.model_dump(mode='json'))
    except ValidationError as exc:
        raise WorkflowError('确认后的字段结构无效，请检查字段重名、数量或业务规则：' + str(exc.errors()[0]['msg'])) from exc
    fields = {f.rule.name: f for f in config.fields}
    row_choices = {r.key: r for r in request.rows}
    if len(row_choices) != len(request.rows) or set(row_choices) - {r['key'] for r in document['rows']}:
        raise WorkflowError('商品对应关系包含重复或未知行。')
    counts = Counter(r['id'] for r in document['rows'] if r['id'])
    sku_map = defaultdict(list)
    for row in old_rows:
        if row['values_json'].get('sku'):
            sku_map[row['values_json']['sku']].append(row['id'])
    provided_existing = sorted(set(mapped.values()) & existing.keys())
    content_index = defaultdict(set)
    for old in old_rows:
        content_index[tuple(old['values_json'].get(k, '') for k in provided_existing)].add(old['id'])
    sheets_without_id = {s['name'] for s in document['sheets'] if s['id_column'] is None}
    proposed, review, assigned = [], [], set()
    diff_locations = {}
    for incoming in document['rows']:
        values = {mapped[k]: v for k, v in incoming['values'].items() if k in mapped}
        if not values and not incoming['id']:
            continue
        rid = incoming['id']
        candidates = sku_map.get(values.get('sku', ''), [])
        if not candidates and not rid:
            candidates = sorted(content_index.get(tuple(values.get(k, '') for k in provided_existing), set()))
        reason = ''
        if rid and rid not in old_by_id:
            reason = '记录 ID 未知，请选择对应商品或明确作为新增'
        elif rid and counts[rid] > 1:
            reason = '记录 ID 重复，可能复制了旧行，请分别确认'
        elif not rid and candidates:
            reason = '没有记录 ID，但货号或商品内容与原记录相符，请确认对应商品或作为新增'
        elif not rid and incoming['sheet'] in sheets_without_id:
            reason = '记录 ID 整列缺失，请确认此行对应商品或明确作为新增'
        elif rid and candidates and rid not in candidates:
            reason = '记录 ID 与货号对应不同商品，可能排序错位，请确认'
        # An empty ID is the documented insertion contract. A SKU is optional:
        # ordinary new rows receive a new ID below, while identity-loss risks
        # (matching old content/SKU or a missing ID column) still require review.
        if not reason and rid and len([v for k, v in values.items() if k in existing and v]) >= 2:
            matching_content = content_index.get(tuple(values.get(k, '') for k in provided_existing), set())
            if matching_content and rid not in matching_content:
                reason = '商品内容与其他原记录一致，可能只排序了业务列，请确认对应商品'
        choice = row_choices.get(incoming['key'])
        if reason and choice is None:
            review.append({'key': incoming['key'], 'sheet': incoming['sheet'], 'row': incoming['row'], 'id': rid,
                           'reason': reason, 'suggested_id': candidates[0] if len(candidates) == 1 else '',
                           'summary': '；'.join(f'{field_label(fields[k])}：{image_text(v)}' for k, v in [(k, v) for k, v in values.items() if v][:12])})
            continue
        action = choice.action if choice else ('existing' if rid else 'new')
        if action == 'ignore':
            continue
        if action == 'existing':
            rid = choice.record_id if choice else rid
            if rid not in old_by_id or rid in assigned:
                raise WorkflowError('同一已有商品只能对应一个回传行，请检查商品对应关系。')
            row = deepcopy(old_by_id[rid])
            assigned.add(rid)
        else:
            row = {'task_id': task['id'], 'id': identifier('row'), 'original': {}, 'source': {'sheet': incoming['sheet'], 'row': incoming['row']},
                   'values_json': {}, 'ordinal': 0, 'issues': [], 'issue_count': 0, 'search_text': ''}
        prior = dict(row['values_json'])
        row['values_json'] = {key: prior.get(key, f.default or '') for key, f in fields.items()}
        for key, value in values.items():
            if len(value) > 32767:
                raise WorkflowError('单元格内容不能超过 32767 字符。')
            if not fields[key].editable and value != row['values_json'][key]:
                raise WorkflowError(f'{field_label(fields[key])} 为只读字段，请恢复原值。')
            row['values_json'][key] = value
        # Incoming geometry is authoritative for provided fields, including unmerge.
        groups = {k: v for k, v in row['source'].get('merged_fields', {}).items() if k in fields and k not in mapped.values()}
        groups.update({mapped[k]: 'return:' + v for k, v in incoming['groups'].items() if k in mapped})
        row['source']['merged_fields'] = groups
        if old_config.source_retention_version>=2:
            row['source']['retained_layout_from_return']=True
            row['source']['rectangular_merges']=[{'fields':[mapped[k] for k in rect['keys'] if k in mapped],'group':'return:'+rect['group']}
                for rect in document.get('rectangles',[]) if rect['sheet']==incoming['sheet'] and rect['start']<=incoming['row']<=rect['end']]
        proposed.append(row)
        diff_locations[row['id']] = {'sheet': incoming['sheet'], 'row': incoming['row']}
    # Preserve old merge identifiers when membership is unchanged, to avoid false diffs.
    old_groups, fresh_groups = defaultdict(set), defaultdict(list)
    for row in old_rows:
        for key, group in row['source'].get('merged_fields', {}).items():
            old_groups[key, group].add(row['id'])
    for row in proposed:
        for key, group in row['source'].get('merged_fields', {}).items():
            if group.startswith('return:'):
                fresh_groups[key, group].append(row)
    for (key, _), members in fresh_groups.items():
        ids = {row['id'] for row in members}
        same = next((group for (field, group), original_ids in old_groups.items() if field == key and original_ids == ids), None)
        if same:
            for row in members:
                row['source']['merged_fields'][key] = same
    old_merge = {(key, frozenset(old_rows[i]['id'] for i in range(start, end + 1))) for key in mapped.values()
                 for start, end in vertical_spans(old_rows, key)}
    new_merge = {(key, frozenset(proposed[i]['id'] for i in range(start, end + 1))) for key in mapped.values()
                 for start, end in vertical_spans(proposed, key)}
    if old_merge != new_merge:
        summary.append(f'数据区合并关系变化：原 {len(old_merge)} 组，回传 {len(new_merge)} 组；合并值应用到覆盖的各条商品')
    def rectangle_signature(rows):
        groups=defaultdict(set)
        for row in rows:
            for rect in row['source'].get('rectangular_merges',[]):
                ids=frozenset(f for f in rect['fields'] if f in mapped.values())
                if len(ids)>1:groups[rect['group'],ids].add(row['id'])
        return {(fields,frozenset(members)) for (_,fields),members in groups.items()}
    if rectangle_signature(old_rows)!=rectangle_signature(proposed):
        summary.append('原表来源区的横向/矩形合并关系变化，请确认说明区域及对应商品行。')
    missing = [r['id'] for r in old_rows if r['id'] not in assigned]
    kept = []
    for old in old_rows:
        if old['id'] not in assigned:
            row = deepcopy(old)
            row['values_json'] = {k: row['values_json'].get(k, f.default or '') for k, f in fields.items()}
            row['source']['merged_fields'] = {k: v for k, v in row['source'].get('merged_fields', {}).items() if k in fields}
            kept.append(row)
    changes, additions, samples = {}, [], []
    for row in proposed:
        if row['id'] not in old_by_id:
            additions.append(row['values_json'])
        else:
            before = old_by_id[row['id']]['values_json']
            delta = {k: v for k, v in row['values_json'].items() if v != before.get(k, '')}
            if delta:
                changes[row['id']] = delta
                if len(samples) < 100:
                    samples.append({'id': row['id'], 'before': {k: before.get(k, '') for k in delta}, 'after': delta})
    old_order = [r['id'] for r in old_rows if r['id'] in assigned]
    if old_order != [r['id'] for r in proposed if r['id'] in assigned]:
        summary.append('商品顺序按回传文件整行顺序调整')
    if not proposed and not kept:
        raise WorkflowError('回传需要至少保留一条商品。')
    if (len(proposed) + len(kept) + 10) * (len(fields) + 1) > MAX_CELLS:
        raise WorkflowError('确认后的维护区域超过 200 万单元格。')
    return {'row_review': review, 'config': config.model_dump(mode='json'), 'proposed': proposed, 'kept': kept, 'missing': missing,
            'diff_locations': diff_locations,
            'diff_columns': {fid: {k: by_key[key][k] for k in ('sheet', 'column')} for key, fid in mapped.items()},
            'structure_summary': summary, 'samples': samples, 'field_options': field_options(config),
            'added_samples': [{'values': v} for v in additions[:20]],
            'missing_samples': [{'id': rid, 'values': old_by_id[rid]['values_json']} for rid in missing[:20]],
            'identity_warnings': [], 'counts': {'changed': len(changes), 'added': len(additions), 'missing': len(missing)}}
