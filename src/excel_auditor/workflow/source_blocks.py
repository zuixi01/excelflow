"""Project genuinely independent horizontal tables into separate mapping scopes.

Requires repeated identity/business headers and a completely blank separator.
Physical input cells are never rewritten; local column indices carry provenance.
"""
from __future__ import annotations

from copy import deepcopy
import re
from openpyxl.utils import get_column_letter, coordinate_to_tuple

from ..product_workflow.fixed_template import match_fixed_field, normalize_header


def header_identity(header):
    leaf = header.split(' / ')[-1].strip()
    return match_fixed_field(leaf) or ('product_name' if normalize_header(leaf) == normalize_header('品名') else '')


def split_horizontal_tables(source):
    if source.get('source_block'):
        return [source]
    columns, rows = source['columns'], source['rows']
    width = len(columns)
    name_columns = [c['column'] for c in columns if header_identity(c['header']) == 'product_name']
    if len(name_columns) < 2:
        return [source]
    # Empty separators must be blank in the actual cells, including embedded
    # image references. An empty header alone is not a separator.
    empty = {c for c in range(1, width+1) if all(c > len(row) or row[c-1] in (None, '') for row in rows)}
    gaps = []
    for left, right in zip(name_columns, name_columns[1:]):
        candidates = [c for c in range(left+1, right) if c in empty]
        if not candidates:
            return [source]
        # Use the last blank run preceding the next independent name header.
        end = candidates[-1];start = end
        while start-1 in empty and start-1 > left:
            start -= 1
        gaps.append((start,end))
    bounds = []
    start = 1
    for first,last in gaps:
        bounds.append((start, first-1));start = last+1
    bounds.append((start,width))
    cores = [{header_identity(c['header']) for c in columns[left-1:right]} - {''} for left,right in bounds]
    required = {'product_name', 'sales_unit', 'sale_price', 'market_price', 'sku'}
    if any('product_name' not in core or len(core & required) < 2 for core in cores):
        return [source]
    if any(len(core & cores[0] & required) < 2 for core in cores[1:]):
        return [source]
    result = []
    original = source.get('source_sheet',source['sheet'])
    for number,(left,right) in enumerate(bounds,start=1):
        label = ('左区' if number==1 else '右区') if len(bounds)==2 else f'区域{number}'
        area = f'{get_column_letter(left)}:{get_column_letter(right)}'
        item = deepcopy(source)
        item['sheet'] = f'{source["sheet"]} · {label} {area}'
        item['source_sheet'] = original
        item['source_block'] = {'index':number,'label':label,'start_column':left,'end_column':right,'range':area}
        item['rows'] = [row[left-1:right] for row in rows]
        while item['rows'] and not any(item['rows'][-1]):item['rows'].pop()
        if item.get('presentation'):
            item['presentation']['cells'] = [row[left-1:right] for row in item['presentation']['cells'][:len(item['rows'])]]
            item['presentation']['row_heights'] = item['presentation']['row_heights'][:len(item['rows'])]
        item['columns'] = [{**column,'column':index,'source_column':column.get('source_column',column['column'])}
                           for index,column in enumerate(columns[left-1:right],start=1)]
        item['widths'] = source.get('widths',[])[left-1:right]
        item['merged_ranges'] = [[a,b,max(c,left)-left+1,min(d,right)-left+1]
                                 for a,b,c,d in source.get('merged_ranges',[]) if c<=right and d>=left]
        # Notes and repeated headers inside one block must not turn into products
        # merely because the neighbouring block contains a valid product row.
        excluded = set(source.get('excluded_rows',[]));notes=[]
        headers = [normalize_header(c['header']) for c in item['columns']]
        first_data = item['header_row']+item['header_depth']
        for row_number,row in enumerate(item['rows'],start=1):
            if row_number < first_data:
                continue
            values = list(dict.fromkeys(str(v).strip() for v in row if v not in (None, '') and str(v).strip()))
            repeated = sum(bool(value) and normalize_header(value)==headers[c] for c,value in enumerate(row))>=2
            note = bool(values) and all(re.match(r'^(备注|说明|以上|注[:：])',v) for v in values)
            if not values or repeated or note:
                excluded.add(row_number)
                if values:
                    notes.append({'row':row_number,'reason':'重复表头' if repeated else '区块说明','values':row})
        item['excluded_rows'] = sorted(excluded)
        item['block_notes'] = notes
        item['row_count'] = sum(any(row) and r not in excluded for r,row in enumerate(item['rows'],start=1) if r>=first_data)
        item['preview'] = item['rows'][:8]
        for member in ('image_locations','formula_results'):
            projected=[]
            for location in source.get(member,[]):
                if not location.get('cell'):
                    continue
                r,c=coordinate_to_tuple(location['cell'])
                if left<=c<=right:
                    projected.append({**location,'source_cell':location['cell'], 'sheet':item['sheet'],
                                      'cell':f'{get_column_letter(c-left+1)}{r}'})
            item[member]=projected
        result.append(item)
    return result
