"""Content-addressed workbook images, with task-scoped references and access."""
from __future__ import annotations

import hashlib
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from PIL import Image as PillowImage
from openpyxl.drawing.image import Image as ExcelImage
from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, OneCellAnchor
from openpyxl.drawing.xdr import XDRPositiveSize2D
from openpyxl.utils import get_column_letter
from openpyxl.utils.units import pixels_to_EMU

from .formula_values import IMAGE_FORMULA, sheet_parts
from .models import WorkflowError

TOKEN = re.compile(r'image://([a-f0-9]{64})(?![a-f0-9])')
MIME = {'PNG': ('png', 'image/png'), 'JPEG': ('jpeg', 'image/jpeg'), 'GIF': ('gif', 'image/gif'),
        'BMP': ('bmp', 'image/bmp'), 'WEBP': ('webp', 'image/webp')}
REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
XDR = '{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}'
DRAW = '{http://schemas.openxmlformats.org/drawingml/2006/main}'


def image_ids(value):
    return list(dict.fromkeys(TOKEN.findall(str(value or ''))))


def image_text(value):
    return TOKEN.sub('图片', str(value or ''))


def find_asset(directory, digest):
    if not re.fullmatch('[a-f0-9]{64}', digest):
        raise WorkflowError('图片标识无效。', 404)
    metadata = Path(directory) / (digest + '.json')
    if not metadata.is_file():
        raise WorkflowError('图片资源不存在，请保留原图片或重新上传。', 422, 'IMAGE_ASSET_MISSING')
    info = json.loads(metadata.read_text(encoding='utf-8'))
    if info.get('extension') not in {v[0] for v in MIME.values()}:
        raise WorkflowError('图片资源格式无效。')
    path = Path(directory) / (digest + '.' + info['extension'])
    if not path.is_file():
        raise WorkflowError('图片资源缺失，请重新上传。', 422, 'IMAGE_ASSET_MISSING')
    return path, info


def store_asset(directory, data):
    if len(data) > 100 * 1024 * 1024:
        raise WorkflowError('单张图片超过 100 MiB。')
    try:
        with PillowImage.open(BytesIO(data)) as pic:
            if pic.format not in MIME or pic.width * pic.height > 50_000_000:
                raise ValueError('图片格式或像素数超出支持范围')
            ext, mime = MIME[pic.format]
            width, height = pic.size
            pic.verify()
    except Exception as exc:
        raise WorkflowError('工作簿含无法读取的图片，不能丢弃图片后继续导入。', 422, 'IMAGE_INVALID') from exc
    digest = hashlib.sha256(data).hexdigest()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (digest + '.' + ext)
    if not path.exists():
        path.write_bytes(data)
    info = {'id': digest, 'extension': ext, 'mime_type': mime, 'width': width, 'height': height, 'size': len(data)}
    meta = directory / (digest + '.json')
    if not meta.exists():
        meta.write_text(json.dumps(info), encoding='utf-8')
    return info


def related_part(base, target):
    if '\\' in target or '?' in target or '#' in target or '://' in target:
        raise WorkflowError('图片关系包含无效路径。')
    path = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join(posixpath.dirname(base), target))
    if not path.startswith('xl/') or '..' in PurePosixPath(path).parts:
        raise WorkflowError('图片关系超出工作簿范围。')
    return path


def relationships(archive, part):
    relpath = posixpath.join(posixpath.dirname(part), '_rels', posixpath.basename(part) + '.rels')
    if relpath not in archive.namelist():
        return {}
    result = {}
    for rel in ET.fromstring(archive.read(relpath)):
        if rel.attrib.get('TargetMode') == 'External':
            # Ignore unused external relationships. A referenced picture without
            # an embedded resource is rejected at its actual cell below.
            continue
        result[rel.attrib['Id']] = related_part(part, rel.attrib['Target'])
    return result


def extract_images(path, book, directory, *, active_sheets=None):
    """Extract WPS DISPIMG and ordinary drawing anchors; keep every media asset."""
    cells, assets, locations = {}, {}, []
    with ZipFile(path) as archive:
        def asset(part):
            if part not in assets:
                if part not in archive.namelist():
                    raise WorkflowError('工作簿引用的图片文件不存在。', 422, 'IMAGE_ASSET_MISSING')
                assets[part] = store_asset(directory, archive.read(part))
            return assets[part]['id']
        # Keep header/logos and unused original media too, in the image archive.
        for part in archive.namelist():
            if part.startswith('xl/media/') and not part.endswith('/'):
                asset(part)
        cell_images = {}
        for part in archive.namelist():
            if not part.lower().endswith('/cellimages.xml'):
                continue
            rels = relationships(archive, part)
            for picture in ET.fromstring(archive.read(part)).iter(XDR+'pic'):
                identity = picture.find('.//' + XDR+'cNvPr')
                blip = picture.find('.//' + DRAW+'blip')
                if identity is not None and blip is not None and blip.attrib.get(REL+'embed') in rels:
                    cell_images[identity.attrib.get('name')] = asset(rels[blip.attrib[REL+'embed']])
        for title, sheet_part in sheet_parts(archive).items():
            if title not in book.sheetnames or active_sheets is not None and title not in active_sheets:
                continue
            sheet = book[title]
            for row in sheet:
                for cell in row:
                    match = IMAGE_FORMULA.fullmatch(cell.value or '') if cell.data_type == 'f' and isinstance(cell.value, str) else None
                    if match:
                        digest = cell_images.get(match[1])
                        if not digest:
                            raise WorkflowError(f'{title}!{cell.coordinate} 图片公式找不到对应的内嵌图片。', 422, 'IMAGE_ASSET_MISSING')
                        cells.setdefault((title, cell.coordinate), []).append(digest)
                        locations.append({'sheet': title, 'cell': cell.coordinate, 'image': digest, 'kind': 'cell_image'})
            rels = relationships(archive, sheet_part)
            xml = ET.fromstring(archive.read(sheet_part))
            for drawing in xml.findall('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}drawing'):
                part = rels.get(drawing.attrib.get(REL+'id'))
                if part is None:
                    raise WorkflowError('工作表绘图引用缺失，无法保留图片。')
                drawing_rels = relationships(archive, part)
                for anchor in ET.fromstring(archive.read(part)):
                    start = anchor.find(XDR+'from')
                    for picture in anchor.iter(XDR+'pic'):
                        blip = picture.find('.//' + DRAW+'blip')
                        image_part = drawing_rels.get(blip.attrib.get(REL+'embed')) if blip is not None else None
                        if image_part is None:
                            raise WorkflowError('绘图图片缺少内嵌资源。')
                        digest = asset(image_part)
                        if start is not None:
                            row, col = int(start.find(XDR+'row').text)+1, int(start.find(XDR+'col').text)+1
                            if not 1 <= row <= 20100 or not 1 <= col <= 10201:
                                raise WorkflowError('图片锚点超出当前数据区域容量。')
                            coordinate = f'{get_column_letter(col)}{row}'
                            cells.setdefault((title, coordinate), []).append(digest)
                            locations.append({'sheet': title, 'cell': coordinate, 'image': digest, 'kind': 'drawing'})
                        else:
                            locations.append({'sheet': title, 'cell': None, 'image': digest, 'kind': 'absolute_drawing'})
    return cells, {'assets': list({info['id']: info for info in assets.values()}.values()), 'locations': locations}


def resolve_image_cells(book, cells):
    for (title, coordinate), digests in cells.items():
        cell = book[title][coordinate]
        # A floating logo may cover an existing header/price. Keep that literal
        # content and archive the picture; don't corrupt its business type.
        if cell.value is None or cell.data_type == 'f' and IMAGE_FORMULA.fullmatch(str(cell.value)):
            cell.value = '\n'.join('image://' + d for d in dict.fromkeys(digests))
            cell.data_type = 's'


def copy_assets(source, destination):
    source, destination = Path(source), Path(destination)
    if not source.is_dir():
        return
    for metadata in source.glob('*.json'):
        if not re.fullmatch('[a-f0-9]{64}', metadata.stem):
            continue
        path, info = find_asset(source, metadata.stem)
        # Validate immutable bytes before bringing a returned asset into a task.
        if hashlib.sha256(path.read_bytes()).hexdigest() != metadata.stem:
            raise WorkflowError('图片内容校验失败。')
        destination.mkdir(parents=True, exist_ok=True)
        for file in (path, metadata):
            if not (destination / file.name).exists():
                shutil.copyfile(file, destination / file.name)


def record_image_ids(rows):
    return sorted({digest for row in rows for value in row['values_json'].values() for digest in image_ids(value)})


def validate_image_refs(rows, directory):
    for digest in record_image_ids(rows):
        find_asset(directory, digest)


def snapshot_assets(source, destination, digests):
    destination = Path(destination)
    if digests:
        destination.mkdir(parents=True, exist_ok=True)
    result = []
    for digest in digests:
        path, info = find_asset(source, digest)
        for file in (path, path.with_suffix('.json')):
            if not (destination / file.name).exists():
                shutil.copyfile(file, destination / file.name)
        result.append(info)
    return result


def embed_images(sheet, directory, first_data_row):
    cells = [cell for row in sheet.iter_rows(min_row=first_data_row) for cell in row if image_ids(cell.value)]
    for cell in cells:
        digests = image_ids(cell.value)
        width = max(12, int((sheet.column_dimensions[cell.column_letter].width or 13)*7)-8)
        height = max(12, int((sheet.row_dimensions[cell.row].height or 36)*96/72)-6)
        for index, digest in enumerate(digests):
            if directory is None:
                raise WorkflowError('导出图片资源目录缺失。')
            path, info = find_asset(directory, digest)
            pic = ExcelImage(str(path))
            scale = min(width/len(digests)/info['width'], height/info['height'])
            w, h = max(1, int(info['width']*scale)), max(1, int(info['height']*scale))
            pic.anchor = OneCellAnchor(_from=AnchorMarker(col=cell.column-1, row=cell.row-1,
                          colOff=pixels_to_EMU(3+int(index*width/len(digests))), rowOff=pixels_to_EMU(3)),
                          ext=XDRPositiveSize2D(pixels_to_EMU(w), pixels_to_EMU(h)))
            sheet.add_image(pic)
        if not TOKEN.sub('', str(cell.value)).strip():
            # Keep a sortable/copyable reference underneath the drawing, invisible
            # in the grid. It never adds helper columns or changes header layout.
            cell.number_format = ';;;'
        else:
            cell.value = image_text(cell.value)


def deduplicate_images(path):
    """Share identical media parts across anchors, retaining every drawing.

    openpyxl normally writes the same bitmap again for every repeated product.
    Sharing the media prevents a valid maintenance download exceeding its return
    upload limit solely because a picture appears on multiple product rows.
    """
    temporary = path.with_suffix('.images.tmp')
    try:
        with ZipFile(path) as source:
            canonical, aliases = {}, {}
            for name in source.namelist():
                if name.startswith('xl/media/') and not name.endswith('/'):
                    digest = hashlib.sha256(source.read(name)).hexdigest()
                    if digest in canonical:
                        aliases[name] = canonical[digest]
                    else:
                        canonical[digest] = name
            if not aliases:
                return
            with ZipFile(temporary, 'w') as target:
                for info in source.infolist():
                    if info.filename in aliases:
                        continue
                    data = source.read(info.filename)
                    if info.filename.endswith('.rels'):
                        root = ET.fromstring(data)
                        base = posixpath.join(posixpath.dirname(posixpath.dirname(info.filename)), posixpath.basename(info.filename)[:-5])
                        changed = False
                        for rel in root:
                            if rel.attrib.get('Type', '').endswith('/image') and rel.attrib.get('TargetMode') != 'External':
                                original = related_part(base, rel.attrib['Target'])
                                if original in aliases:
                                    rel.attrib['Target'] = posixpath.relpath(aliases[original], posixpath.dirname(base))
                                    changed = True
                        if changed:
                            data = ET.tostring(root, encoding='utf-8', xml_declaration=True)
                    target.writestr(info, data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
