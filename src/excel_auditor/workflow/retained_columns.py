"""Task-local source columns, kept separate from the published field profile."""
from __future__ import annotations

import hashlib
import re

from openpyxl.utils import get_column_letter

from ..models import ColumnRule
from .models import MaintenanceField, OutputColumn, SourceColumn, WorkflowConfig


def is_sequence_header(header: str) -> bool:
    # Match only explicit row counters, never 商品编号/货号/型号.
    return header.strip() in {"序号", "行号"}


def standard_fields_only(config: WorkflowConfig) -> WorkflowConfig:
    result = config.model_copy(deep=True)
    retained = {field.rule.name for field in result.fields if field.source_column is not None}
    result.fields = [field for field in result.fields if field.rule.name not in retained]
    for file in result.outputs:
        for sheet in file.sheets:
            sheet.columns = [column for column in sheet.columns if column.field not in retained]
    return result


def retain_source_columns(config: WorkflowConfig, sources: list[dict], mappings) -> WorkflowConfig:
    """Rebuild extras in workbook order, using positions rather than header names as identity."""
    result = standard_fields_only(config)
    plans = {plan.sheet: plan for plan in mappings}
    occupied = {field.rule.name for field in result.fields}
    for source in sources:
        plan = plans[source["sheet"]]
        if not plan.include:
            continue
        used = {column for binding in plan.bindings if binding.operation != "constant" for column in binding.columns}
        used -= set(source.get('verbatim_columns', []))
        context_columns = {i for number in source.get('context_rows', {})
                           for i,v in enumerate(source['rows'][int(number)-1],1) if v}
        # Older source snapshots can still include whitespace-only trailing columns.
        # Preserve internal spacers and named-but-empty fields; only trim the tail.
        last_content_column = max((i for row in source['rows'] for i, value in enumerate(row, 1)
                                   if value is not None and not (isinstance(value, str) and not value.strip())), default=0)
        for column in source["columns"]:
            position = column["column"]
            if position > last_content_column:
                continue
            if position in used and (result.source_retention_version < 2 or position not in context_columns):
                continue
            # Read the selected header cells before header matching's NFKC normalization.
            parts = []
            for row in source["rows"][plan.header_row - 1:plan.header_row + plan.header_depth - 1]:
                value = row[position - 1] if position <= len(row) else ""
                if value and (not parts or parts[-1] != value):
                    parts.append(value)
            header = " / ".join(parts)
            if result.exclude_source_sequence and is_sequence_header(header) and position not in context_columns:
                continue
            origin = SourceColumn(sheet=source["sheet"], column=position, header=header,
                                  context_only=result.source_retention_version >= 2 and (position in used or result.exclude_source_sequence and is_sequence_header(header)))
            digest = hashlib.sha256(source["sheet"].encode()).hexdigest()[:16]
            name = f"source_extra.{digest}.{position}"
            while name in occupied:
                name += "_"
            occupied.add(name)
            # Display IDs remain bounded; the full literal header lives in origin metadata.
            label = re.sub(r"[\x00-\x1f/]", " ", header).strip() or f"未命名列 {position}"
            title = f"原表保留列/{source['sheet']} · {get_column_letter(position)}/{label[:160]}"
            field = MaintenanceField(rule=ColumnRule(name=name, title=title, type="string"), source_column=origin)
            if result.source_retention_version >= 2:
                if result.source_header_layout=='grouped':
                    field.header_path = [source.get('source_group_title') or source['sheet'], *(parts or [f'未命名列 {position}'])]
                widths = source.get('widths', [])
                if position <= len(widths):field.maintenance_width = min(255, max(0, widths[position-1]))
            result.fields.append(field)
            for file in result.outputs:
                for sheet in file.sheets:
                    sheet.columns.append(OutputColumn(title=title, field=name))
    return WorkflowConfig.model_validate(result.model_dump(mode="json"))


def field_header_path(field: MaintenanceField, *, layout: str = "provenance") -> list[str]:
    if field.header_path:
        return field.header_path
    if field.source_column is not None:
        source = field.source_column
        if layout == "grouped":
            return [source.sheet, source.header or f"未命名列 {source.column}"]
        if layout == "compact":
            return [source.header or f"未命名列 {source.column}"]
        return ["原表保留列", f"{source.sheet} · {get_column_letter(source.column)}",
                source.header or f"未命名列 {source.column}"]
    return [part.strip() for part in field.rule.title.split("/") if part.strip()] or [field.rule.title]
