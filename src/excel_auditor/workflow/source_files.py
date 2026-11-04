"""Read each supplier workbook independently, with task-wide limits and provenance."""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .excel import MAX_CELLS, MAX_ROWS, MAX_UPLOAD, discover
from .uploads import size
from .models import WorkflowError
from .limits import MAX_TOTAL_UPLOAD, MAX_UPLOAD_MIB, MAX_TOTAL_UPLOAD_MIB
from .source_blocks import split_horizontal_tables

MAX_SOURCE_FILES = 30
MAX_SOURCE_SHEETS = 50


def clean_filename(filename: str) -> str:
    name = re.split(r"[/\\]", filename)[-1]
    return re.sub(r"[\x00-\x1f]", "", name)[:150] or "供应商.xlsx"


def validate_uploads(files: list[tuple[str, bytes]]) -> None:
    if not 1 <= len(files) <= MAX_SOURCE_FILES:
        raise WorkflowError(f"每个任务请选择 1 至 {MAX_SOURCE_FILES} 个 Excel 文件。")
    if any(size(content) > MAX_UPLOAD for _, content in files):
        raise WorkflowError(f"单个 Excel 文件不能超过 {MAX_UPLOAD_MIB} MiB，请拆分文件后上传。", 413)
    if sum(size(content) for _, content in files) > MAX_TOTAL_UPLOAD:
        raise WorkflowError(f"所选 Excel 文件合计不能超过 {MAX_TOTAL_UPLOAD_MIB} MiB，请拆分任务上传。", 413)


def discover_sources(directory: Path, sources: list[dict], config) -> list[dict]:
    sheets = []
    total_rows = total_cells = 0
    multiple = len(sources) > 1
    filename_counts = Counter(Path(source['filename']).stem.casefold() for source in sources)
    for file_index, source in enumerate(sources, start=1):
        try:
            discovered = discover(directory / source['storage_name'], config.fields, supplier=True,
                                  preserve_extras=config.preserve_extras, faithful=config.source_retention_version >= 2)
            discovered = [block for sheet in discovered for block in split_horizontal_tables(sheet)]
        except WorkflowError as exc:
            raise WorkflowError(f"{source['filename']}：{exc}", exc.status, exc.code) from exc
        for sheet_index, sheet in enumerate(discovered, start=1):
            original_sheet = sheet.get('source_sheet',sheet['sheet'])
            display_sheet = sheet['sheet']
            group_title = Path(source['filename']).stem
            if len(discovered) > 1:
                group_title += f' · {display_sheet}'
            if filename_counts[Path(source['filename']).stem.casefold()] > 1:
                group_title = f'{file_index}. {group_title}'
            # Display provenance is separate from the stable sheet identity used by mappings/IDs.
            sheet.update(source_filename=source['filename'], source_sheet=original_sheet,
                         source_file_index=file_index, source_group_title=group_title)
            if multiple:
                # Ordinals guarantee uniqueness even for identical file and sheet names.
                # Bounded display names also leave room for retained-column titles.
                stem = Path(source['filename']).stem[:24]
                sheet['sheet'] = f"{file_index}.{sheet_index} {stem} · {display_sheet[:45]}"
            total_rows += max(0, len(sheet['rows']) - sheet['header_row'] - sheet['header_depth'] + 1)
            total_cells += len(sheet['rows']) * len(sheet['columns'])
            sheets.append(sheet)
            if len(sheets) > MAX_SOURCE_SHEETS:
                raise WorkflowError("所选文件合计最多支持 50 个有效工作表，请拆分任务。")
            if total_rows > MAX_ROWS:
                raise WorkflowError("所选文件合计最多支持 20,000 条记录，请拆分任务。")
            if total_cells > MAX_CELLS:
                raise WorkflowError("所选文件合计数据区域超过 200 万单元格，请拆分任务。")
    return sheets
