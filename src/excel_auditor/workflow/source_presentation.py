"""Literal source cells, including non-product rows and rectangular merges."""
from __future__ import annotations

from copy import copy
import re
from openpyxl.cell.cell import MergedCell
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill, GradientFill, Border, Alignment, Protection
from openpyxl.xml.functions import tostring, fromstring


def capture_presentation(sheet, height, width):
    palette=[];lookup={};cells=[]
    for row in sheet.iter_rows(max_row=height,max_col=width):
        line=[]
        for cell in row:
            style={key:tostring(getattr(cell,key).to_tree()).decode() for key in ('font','fill','border','alignment','protection')}
            style['number_format']=cell.number_format
            key=tuple(style.items())
            if key not in lookup:
                lookup[key]=len(palette);palette.append(style)
            line.append(lookup[key])
        cells.append(line)
    return {'styles':palette,'cells':cells,'row_heights':[sheet.row_dimensions[r].height for r in range(1,height+1)]}


def context_rows(source, mapping):
    """Only positively identified notes, repeated labels and blank rows.

    A missing product name is never sufficient to call a product a note.
    """
    result={}
    start=mapping.header_row+mapping.header_depth
    fee=not any(b.field=='product_name' for b in mapping.bindings) and bool(re.search(r'费用|收费标准|吊装费',source.get('source_sheet',source['sheet'])))
    headers=[c['header'].split(' / ')[-1].strip() for c in source['columns']]
    for number,row in enumerate(source['rows'],1):
        if number < start:
            continue
        values=list(dict.fromkeys(str(v).strip() for v in row if str(v).strip()))
        if not values:
            result[number]='原表空行';continue
        if fee:
            result[number]='费用/服务说明表';continue
        identity=False
        for binding in mapping.bindings:
            if binding.field not in {'product_name','sku'}:continue
            if binding.operation=='constant':identity=identity or bool(binding.argument.strip())
            else:identity=identity or any(c<=len(row) and str(row[c-1]).strip() for c in binding.columns)
        if any(v in {'合计','总计','小计'} for v in values) and not identity and not any(str(v).startswith('image://') for v in row):
            result[number]='原表汇总/合计';continue
        if sum(bool(v) and str(v).strip()==headers[c] for c,v in enumerate(row))>=2 or ('型号' in values and '价格' in values):
            result[number]='原表重复表头';continue
        merged_note=any(a<=number<=b and d>c and len(values)==1 and (len(values[0])>=25 or re.match(r'^\s*\d+[、.．)]',values[0]))
                        for a,b,c,d in source.get('merged_ranges',[]))
        if (len(values)==1 and re.match(r'^(备注|说明|以上|注[:：]|合计|总计)',values[0])) or merged_note:
            result[number]='原表合并说明' if merged_note else '原表说明/合计'
    return result


def is_context(record, config):
    return record.get('source',{}).get('kind')=='context' and not any(
        record['values_json'].get(f.rule.name) for f in config.fields if f.source_column is None)


def source_presentations(sources, plans):
    by_sheet={p.sheet:p for p in plans if p.include}
    return {s['sheet']:{'presentation':s.get('presentation'), 'merged_ranges':s.get('merged_ranges',[]),
                       'header_row':by_sheet[s['sheet']].header_row,'header_depth':by_sheet[s['sheet']].header_depth,
                       'top_notes':[{'row':r,'values':s['rows'][r-1]} for r in range(1,by_sheet[s['sheet']].header_row)]}
            for s in sources if s['sheet'] in by_sheet}


def attach_retained_rectangles(rows,sources,plans,config):
    by_sheet={p.sheet:p for p in plans if p.include}
    for source in sources:
        plan=by_sheet.get(source['sheet'])
        if not plan:continue
        for a,b,c,d in source.get('merged_ranges',[]):
            if c==d or a<plan.header_row+plan.header_depth:continue
            fields=[f.rule.name for f in config.fields if f.source_column and f.source_column.sheet==source['sheet'] and c<=f.source_column.column<=d]
            if len(fields)<2:continue
            group=f"source:{source['sheet']}:{a}:{b}:{c}:{d}"
            for row in rows:
                if row['source'].get('sheet')==source['sheet'] and a<=row['source'].get('row',0)<=b:
                    active=[fid for fid in fields if not next(f for f in config.fields if f.rule.name==fid).source_column.context_only or row['source'].get('kind')=='context']
                    values={row['values_json'].get(fid,'') for fid in active}
                    if len(active)<2 or len(values)!=1 or not next(iter(values),''):continue
                    positions={f.rule.name:i for i,f in enumerate(config.fields)}
                    chunks=[]
                    for fid in active:
                        if not chunks or positions[fid]!=positions[chunks[-1][-1]]+1:chunks.append([])
                        chunks[-1].append(fid)
                    for chunk in chunks:
                        if len(chunk)<2:continue
                        key=group+':'+chunk[0]
                        row['source'].setdefault('rectangular_merges',[]).append({'fields':chunk,'group':key})
                        if b>a:row['source'].setdefault('merged_fields',{}).update({fid:key+':'+fid for fid in chunk})


def apply_retained_presentation(sheet, rows, config, columns, sources, start, header_depth):
    """Project only source fields. Never merge across unrelated business fields.

    Rectangles are split when rows/columns have been reordered, or values
    disagree, so a merge can never discard a separate operator edit.
    """
    if not sources:
        return
    parsed={}
    def style_cell(cell, style):
        if isinstance(cell,MergedCell):return
        key=id(style)
        if key not in parsed:
            parsed[key]={name:klass.from_tree(fromstring(style[name])) for name,klass in
                         [('font',Font),('fill',GradientFill if 'gradientFill' in style['fill'] else PatternFill),
                          ('border',Border),('alignment',Alignment),('protection',Protection)]}
        for name,value in parsed[key].items():setattr(cell,name,copy(value))
        # Maintenance cells contain literal text even if the source displayed numbers.
        cell.number_format=style['number_format']
    origins={f.rule.name:f.source_column for f in config.fields if f.source_column and f.rule.name in columns}
    for name,origin in origins.items():
        metadata=sources.get(origin.sheet,{})
        p=metadata.get('presentation')
        if not p:continue
        col=columns[name]
        for offset,record in enumerate(rows):
            if record['source'].get('sheet')!=origin.sheet or origin.context_only and record['source'].get('kind')!='context':continue
            r=record['source'].get('row',0)-1;c=origin.column-1
            if 0<=r<len(p['cells']) and c<len(p['cells'][r]):
                style_cell(sheet.cell(start+offset,col),p['styles'][p['cells'][r][c]])
                if record['source'].get('kind')=='context' and not isinstance(sheet.cell(start+offset,col),MergedCell):
                    sheet.cell(start+offset,col).comment=Comment(f"{record['source'].get('context_reason','原表说明')}；原表第{r+1}行，非商品字段。",'ExcelFlow')
                if p['row_heights'][r] is not None:
                    sheet.row_dimensions[start+offset].height=max(sheet.row_dimensions[start+offset].height or 0,p['row_heights'][r])
        # The source group remains the top level; style only original header labels.
        for offset in range(metadata['header_depth'] if config.source_header_layout=='grouped' and header_depth>=2 else 0):
            r=metadata['header_row']+offset-1
            if r>=len(p['cells']) or origin.column>len(p['cells'][r]):continue
            target=max(2,header_depth-metadata['header_depth']+1+offset)
            cell=sheet.cell(target,col)
            if isinstance(cell,MergedCell):
                region=next((m for m in sheet.merged_cells.ranges if m.min_row<=target<=m.max_row and m.min_col<=col<=m.max_col),None)
                if region and region.min_row>=2:cell=sheet.cell(region.min_row,region.min_col)
            style_cell(cell,p['styles'][p['cells'][r][origin.column-1]])
    def runs(items):
        current=[]
        for physical,target in sorted(items):
            if current and (physical!=current[-1][0]+1 or target!=current[-1][1]+1):
                yield current;current=[]
            current.append((physical,target))
        if current:yield current
    rectangles={}
    for i,record in enumerate(rows):
        for rect in record.get('source',{}).get('rectangular_merges',[]):
            key=(rect['group'],tuple(rect['fields']))
            rectangles.setdefault(key,[]).append((i,start+i))
    for (_group,fields),row_positions in rectangles.items():
        field_positions=sorted(columns[f] for f in fields if f in origins)
        for rr in runs(row_positions):
                for cc in runs([(c,c) for c in field_positions]):
                    top,bottom=rr[0][1],rr[-1][1];left,right=cc[0][1],cc[-1][1]
                    if top==bottom and left==right:continue
                    values={str(sheet.cell(r,k).value or '') for r in range(top,bottom+1) for k in range(left,right+1) if not isinstance(sheet.cell(r,k),MergedCell)}
                    if len(values)!=1 or not next(iter(values),''):continue
                    for region in list(sheet.merged_cells.ranges):
                        if region.min_row>=top and region.max_row<=bottom and region.min_col>=left and region.max_col<=right:
                            sheet.unmerge_cells(str(region))
                    sheet.merge_cells(start_row=top,end_row=bottom,start_column=left,end_column=right)
