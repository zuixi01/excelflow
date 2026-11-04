"""Resolve supplier images and verified formula results in memory only."""
from .formula_values import FormulaValues
from .image_assets import extract_images, resolve_image_cells


def resolve_workbook(book, path, *, active_sheets=None):
    directory = path.parent / 'images'
    cells, manifest = extract_images(path, book, directory, active_sheets=active_sheets)
    resolve_image_cells(book, cells)
    evaluator = FormulaValues(book, path)
    pending = []
    for sheet in book:
        if active_sheets is not None and sheet.title not in active_sheets:
            continue
        for row in sheet:
            for cell in row:
                if cell.data_type == 'f':
                    pending.append((sheet.title, cell.coordinate))
    # Resolve first, replace second: dependent formulas see original cell types.
    values = [(title, coordinate, evaluator.resolve(title, coordinate)) for title, coordinate in pending]
    for title, coordinate, value in values:
        cell = book[title][coordinate]
        cell.value = value
        cell.data_type = 's'
    return manifest, [{'sheet': title, **report} for (title, _), report in evaluator.reports.items()]
