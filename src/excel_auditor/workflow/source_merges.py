"""Preserve shared source cells without merging independent product records."""
from __future__ import annotations

import hashlib
from collections import defaultdict

from .models import WorkflowError


def shared_row_blocks(regions):
    """Partition actual vertical merges at every source boundary.

    Nested or staggered merges must not make an added field span across a
    smaller source block. No grouping is inferred from equal cell contents.
    """
    events = defaultdict(int)
    for start, end, _left, _right in regions:
        events[start] += 1
        events[end + 1] -= 1
    boundaries = sorted(events)
    active = 0
    for start, stop in zip(boundaries, boundaries[1:]):
        active += events[start]
        if active and stop - start > 1:
            yield start, stop - 1


def attach_source_merges(rows, sources, mappings, config):
    if not config.preserve_source_merges:
        return
    plans = {plan.sheet: plan for plan in mappings if plan.include}
    by_source = {(row['source']['sheet'], row['source']['row']): row for row in rows}
    for source in sources:
        plan = plans.get(source['sheet'])
        if plan is None:
            continue
        fields_by_column = defaultdict(set)
        sourced_fields = set()
        for binding in plan.bindings:
            if binding.columns and binding.operation != 'constant':
                sourced_fields.add(binding.field)
            if len(binding.columns) == 1 and binding.operation != 'constant':
                fields_by_column[binding.columns[0]].add(binding.field)
        for field in config.fields:
            origin = field.source_column
            if origin and origin.sheet == source['sheet']:
                fields_by_column[origin.column].add(field.rule.name)
                sourced_fields.add(field.rule.name)
        digest = hashlib.sha256(source['sheet'].encode()).hexdigest()[:16]
        regions = [region for region in source.get('merged_ranges', [])
                   if region[0] > plan.header_row + plan.header_depth - 1
                   and region[0] < region[1] and region[2] == region[3]]
        # Fields with no source cell (including blank platform fields) belong
        # to the shared source block. Explicit source columns always retain
        # their own merge geometry, even when their values happen to be equal.
        shared_fields = {field.rule.name for field in config.fields} - sourced_fields
        for start, end in shared_row_blocks(regions):
            group = f'{digest}:shared:{start}:{end}'
            for index in range(start, end + 1):
                row = by_source.get((source['sheet'], index))
                if row is not None and shared_fields:
                    row['source'].setdefault('merged_fields', {}).update(
                        {field: group for field in shared_fields})
        for region in regions:
            start, end, left, right = region
            group = f'{digest}:{left}:{start}:{end}'
            for index in range(start, end + 1):
                row = by_source.get((source['sheet'], index))
                if row is not None:
                    for field in fields_by_column[left]:
                        row['source'].setdefault('merged_fields', {})[field] = group


def merge_key(row, field):
    return row.get('source', {}).get('merged_fields', {}).get(field)


def vertical_spans(rows, field):
    """Only adjacent members of an actual source merge with equal values may merge."""
    start = 0
    while start < len(rows):
        group = merge_key(rows[start], field)
        end = start + 1
        value = rows[start]['values_json'].get(field, '')
        if group:
            while (end < len(rows) and merge_key(rows[end], field) == group
                   and rows[end]['values_json'].get(field, '') == value):
                end += 1
        if end - start > 1:
            yield start, end - 1
        start = end


def expand_shared_changes(rows, changes, deletions=()):
    """An edit to one shared cell updates all its surviving members atomically."""
    by_id = {row['id']: row for row in rows}
    members = defaultdict(list)
    for row in rows:
        for field, group in row.get('source', {}).get('merged_fields', {}).items():
            members[(field, group, row['values_json'].get(field, ''))].append(row['id'])
    expanded = {key: dict(value) for key, value in changes.items()}
    removed = set(deletions)
    rectangles=defaultdict(list)
    for row in rows:
        for rect in row.get('source',{}).get('rectangular_merges',[]):
            for field in rect['fields']:
                rectangles[rect['group'],row['values_json'].get(field,'')].append((row['id'],field))
    assigned_rectangles={}
    for row_id,delta in changes.items():
        for field,value in delta.items():
            row=by_id[row_id]
            for rect in row.get('source',{}).get('rectangular_merges',[]):
                if field not in rect['fields']:continue
                key=rect['group'],row['values_json'].get(field,'')
                if key in assigned_rectangles and assigned_rectangles[key]!=(value or ''):
                    raise WorkflowError('同一个原表说明合并格收到不同修改，请先拆分合并后再分别编辑。')
                assigned_rectangles[key]=value or ''
                for member,target in rectangles[key]:
                    if member not in removed:expanded.setdefault(member,{})[target]=value
    assigned = {}
    for row_id, delta in [(key,dict(value)) for key,value in expanded.items()]:
        row = by_id[row_id]
        for field, value in delta.items():
            group = merge_key(row, field)
            if not group:
                continue
            key = (field, group, row['values_json'].get(field, ''))
            normalized = value or ''
            if key in assigned and assigned[key] != normalized:
                raise WorkflowError('同一个原表合并单元格收到不同修改，请统一该单元格的值后保存。')
            assigned[key] = normalized
            for member in members[key]:
                if member not in removed:
                    expanded.setdefault(member, {})[field] = value
    return expanded
