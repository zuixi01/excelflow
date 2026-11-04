from __future__ import annotations

import re
import unicodedata
from .ai import AIClient
from .excel import field_matches, parse_maintenance_value
from .mapping_runtime import mapping_namespace, mapping_runtime
from .models import WorkflowConfig, WorkflowError
from .retained_columns import is_sequence_header


def text_specification_matches(sheet, field, columns):
    """Recognize unambiguous size/area headings with local value evidence."""
    if not field.rule.name.startswith('local.') or field.rule.type.value != 'string':
        return []
    title = field.rule.title.split('/')[-1]
    aliases = {'尺寸': {'规格', '规格尺寸', '标准尺寸', '标准尺寸(门扇)'},
               '照射面积': {'试用面积', '适用面积', '照明面积'}}.get(title)
    if not aliases:
        return []
    start = sheet.get('header_row', 1) + sheet.get('header_depth', 1) - 1
    pattern = (r'\d+(?:\.\d+)?\s*(?:mm|cm|m|毫米|厘米|米)?\s*[*xX×乘]\s*\d' if title == '尺寸'
               else r'\d+(?:\s*[-~～至]\s*\d+)?\s*(?:m[²2]|㎡|平方|平方米)')
    matches=[]
    for column in columns:
        header=unicodedata.normalize('NFKC', column['header'].split(' / ')[-1]).strip()
        if header not in aliases:
            continue
        if any(index not in sheet.get('excluded_rows', []) and column['column'] <= len(row)
               and re.search(pattern, str(row[column['column']-1]), re.IGNORECASE)
               for index,row in enumerate(sheet.get('rows', [])[start:], start+1)):
            matches.append(column)
    return matches


def warranty_requires_verbatim(sheet, field, column):
    if field.rule.name != 'warranty_days':
        return False
    header = next((c['header'] for c in sheet['columns'] if c['column'] == column), '')
    start = sheet.get('header_row', 1) + sheet.get('header_depth', 1) - 1
    values = [unicodedata.normalize('NFKC', str(row[column-1])).strip()
              for index, row in enumerate(sheet.get('rows', [])[start:], start+1)
              if index not in sheet.get('excluded_rows', []) and column <= len(row) and row[column-1] not in ('', None)]
    explicit_days = '天' in header or '日数' in header
    # A generic duration heading supplies no unit for a bare number. Never
    # silently interpret years, months or a mixed descriptive column as days.
    safe = values and all(re.fullmatch(r'\d+\s*天', v) or explicit_days and re.fullmatch(r'\d+', v) for v in values)
    if safe:
        return False
    sheet['verbatim_columns'] = sorted(set(sheet.get('verbatim_columns', [])) | {column})
    notice = f'“{header}”按原文保留在原表来源列，不自动换算为质保天数。'
    if notice not in sheet['mapping_notices']:
        sheet['mapping_notices'].append(notice)
    return True


def source_type_compatible(sheet, field, column):
    """Reject a header suggestion when every actual value has the wrong type.

    Values are inspected locally, never included in the model request. A
    mixed valid/invalid column still goes through normal maintenance checks.
    """
    if field.rule.type.value not in {'integer', 'decimal', 'boolean', 'date', 'datetime', 'enum', 'json'}:
        return True
    start = sheet.get('header_row', 1) + sheet.get('header_depth', 1) - 1
    nonempty = False
    excluded = set(sheet.get('excluded_rows', []))
    for row_index, row in enumerate(sheet.get('rows', [])[start:], start + 1):
        if row_index in excluded or column > len(row) or row[column - 1] in (None, ''):
            continue
        nonempty = True
        if parse_maintenance_value(row[column - 1], field.rule).valid:
            return True
    return not nonempty


def suggest_semantic_bindings(sheets: list[dict], config: WorkflowConfig, ai: AIClient, categories: list[dict], *, progress=None) -> None:
    settings = ai.settings()
    plans, batches = [], []
    for sheet in sheets:
        sheet.pop('confirmed_mapping_key', None)
        sheet['mapping_notices'] = []
        # Keep the literal warranty text even if an explicit day count is
        # also mapped into the numeric standard field.
        sheet['verbatim_columns'] = [c['column'] for c in sheet['columns'] if re.search(r'质保|保修', c['header'])]
        for column in sheet['verbatim_columns']:
            sheet['mapping_notices'].append(f'“{sheet["columns"][column-1]["header"]}”原文保留在原表来源列。')
        source_columns = [c for c in sheet["columns"] if not config.exclude_source_sequence or not is_sequence_header(c["header"])]
        category_id = sheet.get("category_id", "unclassified")
        fields = [f for f in config.fields if not f.hidden and f.source_column is None and (not config.category_field or not f.category_id or f.category_id == category_id)]
        bindings, notes = [], []
        direct = {c["column"]: set(field_matches(c["header"], fields)) for c in source_columns}
        for field in fields:
            matches = [c for c in source_columns if field.rule.name in direct[c["column"]]
                       or (not direct[c["column"]] and field.rule.name in field_matches(c["header"].split(" / ")[-1], [field]))]
            specification_matches = [] if matches else text_specification_matches(sheet, field, source_columns)
            matches = matches or specification_matches
            if len(matches) == 1:
                if warranty_requires_verbatim(sheet, field, matches[0]['column']):
                    continue
                bindings.append({"field": field.rule.name, "columns": [matches[0]["column"]], "operation": "copy",
                                 "argument": "", "part": 0, "method": "header_rule" if specification_matches else "exact", "confidence": 1.0,
                                 "reason": "尺寸或面积表头与原文格式一致（待人工确认）。" if specification_matches else "名称或已配置别名一致。"})
            elif len(matches) > 1:
                notes.append(f"{field.rule.title} 有多个可能来源，请确认。")
        bound = {b["field"] for b in bindings}
        unresolved = [f for f in fields if f.rule.name not in bound]
        batch_ids = []
        if unresolved and settings["configured"]:
            columns = [{"column": c["column"], "header": c["header"]} for c in source_columns]
            targets = [{"id": f.rule.name, "title": f.rule.title, "description": f.description,
                        "aliases": f.rule.aliases, "type": f.rule.type.value} for f in unresolved]
            batch_ids.append(len(batches))
            batches.append((targets, columns))
        plans.append((sheet, fields, unresolved, bindings, notes, batch_ids))

    # One workbook request, including every unresolved target and source
    # header. Opaque sheet-scoped IDs disambiguate identical column numbers
    # and field names; row values never enter this payload.
    decode = {}
    if len(batches) > 1:
        all_fields, all_columns = [], []
        for sheet, _, _, _, _, ids in plans:
            if not ids:
                continue
            fields, columns = batches[ids[0]]
            scope = f'sheet_{ids[0] + 1}'
            column_ids = {}
            for column in columns:
                index = len(all_columns) + 1
                column_ids[index] = column['column']
                all_columns.append({**column, 'column': index, 'source_column': column['column'],
                                    'sheet_id': scope, 'sheet_name': sheet['sheet']})
            for field in fields:
                field_id = f"{scope}::{field['id']}"
                all_fields.append({**field, 'id': field_id, 'sheet_id': scope, 'sheet_name': sheet['sheet']})
                decode[field_id] = (sheet['sheet'], field['id'], column_ids)
            ids[:] = [0]
        batches = [(all_fields, all_columns)]

    def report(states):
        if progress:
            progress({'stage': 'ai_matching', 'model': settings.get('model', ''), 'total_batches': len(states),
                      'completed_batches': sum(state in {'completed', 'failed'} for state in states),
                      'failed_batches': states.count('failed'),
                      'sheets': [{'sheet': sheet['sheet'], 'total_batches': len(ids),
                                  'completed_batches': sum(states[i] in {'completed', 'failed'} for i in ids),
                                  'failed_batches': sum(states[i] == 'failed' for i in ids),
                                  'status': 'running' if any(states[i] == 'running' for i in ids) else
                                            'completed' if all(states[i] in {'completed', 'failed'} for i in ids) else 'waiting'}
                                 for sheet, _, _, _, _, ids in plans if ids]})
    results = mapping_runtime().analyze(ai, batches, namespace=mapping_namespace(ai, settings), progress=report, use_cache=False) if batches else []
    if progress:
        progress({'stage': 'assembling'})
    for sheet, fields, unresolved, bindings, notes, batch_ids in plans:
        evidence = []
        errors = []
        model_status = "not_needed"
        if batch_ids:
            model_status = "completed"
            for index in batch_ids:
                if isinstance(results[index], Exception):
                    model_status = "failed"
                    error = results[index]
                    messages = {
                        "AI_TIMEOUT": "模型响应超时，自动重试后仍未完成",
                        "AI_SERVICE_ERROR": "模型服务暂时不可用，自动重试后仍未完成",
                        "AI_REQUEST_REJECTED": "模型服务拒绝请求，请检查模型权限、密钥或额度",
                        "AI_INVALID_MAPPING": "模型返回的字段映射格式不正确",
                        "AI_INVALID_RESPONSE": "模型响应格式不正确",
                        "AI_RESPONSE_TOO_LARGE": "模型响应超过大小限制",
                        "AI_ENDPOINT_NOT_ALLOWED": "模型接口不符合当前环境的访问设置",
                        "AI_NOT_CONFIGURED": "模型接口或密钥尚未配置",
                    }
                    code = error.code if isinstance(error, WorkflowError) and error.code in messages else "AI_ANALYSIS_ERROR"
                    errors.append({"batch": batch_ids.index(index) + 1, "code": code,
                                   "message": messages.get(code, "模型分析出现异常，请重新分析或检查服务日志")})
                else:
                    for item in results[index]:
                        if decode:
                            target = decode.get(item['field_id'])
                            if not target or target[0] != sheet['sheet'] or item['column'] not in target[2]:
                                continue
                            item = {**item, 'field_id': target[1], 'column': target[2][item['column']]}
                        evidence.append({"field": item["field_id"], **item})
            for field in unresolved:
                candidates = [e for e in evidence if e["field"] == field.rule.name]
                if len(candidates) == 1:
                    item = candidates[0]
                    if warranty_requires_verbatim(sheet, field, item['column']):
                        continue
                    if item["confidence"] < 0.85:
                        notes.append(f"{field.rule.title} 的语义匹配置信度不足 85%，请手动确认；来源列将完整保留。")
                        continue
                    if not source_type_compatible(sheet, field, item['column']):
                        notes.append(f"{field.rule.title} 的候选来源列内容不符合目标字段类型，请手动确认；来源列将完整保留。")
                        continue
                    bindings.append({"field": field.rule.name, "columns": [item["column"]], "operation": "copy",
                                     "argument": "", "part": 0, "method": "semantic_model",
                                     "confidence": item["confidence"], "reason": item["reason"] + "（待人工确认）"})
                elif len(candidates) > 1:
                    notes.append(f"{field.rule.title} 的语义结果存在多个来源，请手动选择。")
            if model_status == "failed":
                reasons = "；".join(dict.fromkeys(error["message"] for error in errors))
                notes.append(f"部分模型分析未完成（{len(errors)}/{len(batch_ids)} 批）：{reasons}。可重新分析或手动确认字段映射；原始数据保持完整。")
        elif unresolved:
            model_status = "not_configured"
            notes.append("尚未配置语义模型，已匹配同名和别名字段，其余字段可手动确认。")
        if config.category_field and sheet.get("category_id", "unclassified") == "unclassified" and categories:
            notes.append("尚未选择动态字段类目，本工作表仅使用固定字段。")
        bound = {b["field"] for b in bindings}
        unmatched = [{"field": f.rule.name, "title": f.rule.title, "required": f.rule.required}
                     for f in fields if f.rule.name not in bound]
        sheet.update(suggested_bindings=bindings, mapping_notes=notes, semantic_evidence=evidence, unmatched_fields=unmatched, model_errors=errors,
                     mapping_summary={"total": len(fields), "matched": len(bound), "unmatched": len(unmatched), "model_status": model_status})
