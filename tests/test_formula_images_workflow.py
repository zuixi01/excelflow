from io import BytesIO
import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

import pytest
from PIL import Image
from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as DrawingImage

from excel_auditor.workflow.excel import discover, safe_book
from excel_auditor.workflow.field_profiles import ConfiguredField, FieldProfile, FieldProfileService, LocalCategory, ProfileEdit
from excel_auditor.workflow.image_assets import image_ids, store_asset, embed_images, deduplicate_images
from excel_auditor.workflow.models import FlexibleReturnRequest, MappingRequest, WorkflowError, default_config
from excel_auditor.workflow.repository import WorkflowStore
from excel_auditor.workflow.tasks import TaskService


def png(color='red'):
    output=BytesIO()
    Image.new('RGB',(48,24),color).save(output,format='PNG')
    return output.getvalue()


def rewrite(content, files):
    out=BytesIO()
    with ZipFile(BytesIO(content)) as old, ZipFile(out,'w',ZIP_DEFLATED) as new:
        for item in old.infolist():
            new.writestr(item,files.get(item.filename,old.read(item.filename)))
        for name,value in files.items():
            if name not in old.namelist():new.writestr(name,value)
    return out.getvalue()


def workbook_with_images():
    book=Workbook();sheet=book.active;sheet.title='商品'
    sheet.append(['商品名称','货号','商品主图','销售价','品牌','功率'])
    sheet.append(['筒灯','0001','=_xlfn.DISPIMG("ID_pic",1)','=10*1.15','品牌甲','3W'])
    sheet.append(['台灯','0002',None,'=SUM(10,ROUND(2.25,1))','品牌乙','5W'])
    drawing=DrawingImage(BytesIO(png('blue')));drawing.anchor='C3';sheet.add_image(drawing)
    logo=DrawingImage(BytesIO(png('green')));logo.anchor='A1';sheet.add_image(logo)
    output=BytesIO();book.save(output);book.close()
    xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    with ZipFile(BytesIO(output.getvalue())) as archive:
        sheet_xml=ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        sheet_xml.find(f'.//{{{xmlns}}}c[@r="D2"]/{{{xmlns}}}v').text='0'  # deliberately stale
        sheet_xml.find(f'.//{{{xmlns}}}c[@r="D3"]/{{{xmlns}}}v').text='12.3'
        types=ET.fromstring(archive.read('[Content_Types].xml'))
        ET.SubElement(types,'{http://schemas.openxmlformats.org/package/2006/content-types}Override',{'PartName':'/xl/cellimages.xml','ContentType':'application/vnd.wps-officedocument.cellimage+xml'})
    return rewrite(output.getvalue(),{
        'xl/worksheets/sheet1.xml':ET.tostring(sheet_xml),
        '[Content_Types].xml':ET.tostring(types),
        'xl/media/wps.png':png(),
        'xl/cellimages.xml':b'<etc:cellImages xmlns:etc="http://www.wps.cn/officeDocument/2017/etCustomData" xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><etc:cellImage><xdr:pic><xdr:nvPicPr><xdr:cNvPr id="1" name="ID_pic"/></xdr:nvPicPr><xdr:blipFill><a:blip r:embed="rId1"/></xdr:blipFill></xdr:pic></etc:cellImage></etc:cellImages>',
        'xl/_rels/cellimages.xml.rels':b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/wps.png"/><Relationship Id="unused" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="https://unused.invalid/photo.png" TargetMode="External"/></Relationships>'})


def test_images_stale_cache_maintenance_return_and_final_roundtrip(tmp_path):
    store=WorkflowStore(tmp_path/'workflow');service=TaskService(store);profiles=FieldProfileService(store)
    draft=profiles.get('local')
    profile=FieldProfile(fixed_fields=[ConfiguredField(id='product_name',title='商品名称',required=True),
        ConfiguredField(id='sku',title='货号',type='id_code'),ConfiguredField(id='main_image',title='商品主图'),
        ConfiguredField(id='sale_price',title='销售价',type='decimal')],local_categories=[LocalCategory(id='lights',title='灯具',
        attributes=[ConfiguredField(id='brand',title='品牌')],specifications=[ConfiguredField(id='power',title='功率')])])
    saved=profiles.save('local',ProfileEdit(base_revision=draft['revision'],profile=profile));profiles.publish('local',saved['revision'])
    content=workbook_with_images()
    task,_=service.create_standardization('local','operator','图片公式','产品.xlsx',content)
    service.parse(task['id'],'local');task=service.get(task['id'],'local')
    assert task['status']=='mapping',task['payload'].get('error')
    reports=task['payload']['sheets'][0]['formula_results']
    assert reports[0]['status']=='cache_corrected' and reports[0]['cached_value']=='0' and reports[0]['value']=='11.5'
    assert reports[1]['status']=='cache_verified'
    assert len(task['payload']['image_asset_ids'])==3
    task=service.confirm_mapping(task['id'],'local','operator',MappingRequest(base_revision=task['revision'],sheets=[{
        'sheet':s['sheet'],'header_row':s['header_row'],'header_depth':s['header_depth'],'bindings':s['suggested_bindings']} for s in task['payload']['sheets']]))
    assert task['status']=='ready'
    rows=service.rows(task['id'],'local',0,30)['items']
    assert [r['values_json']['sale_price'] for r in rows]==['11.5','12.3']
    assert image_ids(rows[0]['values_json']['main_image'])==[hashlib.sha256(png()).hexdigest()]
    assert image_ids(rows[1]['values_json']['main_image'])==[hashlib.sha256(png('blue')).hexdigest()]
    maintenance=service.maintenance_file(task['id'],'local');book=load_workbook(maintenance)
    sheet=book['维护表'];assert len(sheet._images)==2
    assert sheet['B1'].value=='固定字段' and sheet['F1'].value=='动态字段'
    assert dict(book['_workflow_meta'].values)['header_depth']==3
    assert sheet.column_dimensions['A'].hidden
    book.close()
    inspection=service.inspect_return(task['id'],'local',maintenance.read_bytes())
    choices=[{k:c[k] for k in ('key','action','field_id','title','kind','type')} for c in inspection['payload']['columns']]
    preview=service.preview_flexible_return(task['id'],'local',inspection['id'],FlexibleReturnRequest(columns=choices))
    assert preview['payload']['counts']=={'changed':0,'added':0,'missing':0}
    service.apply_return(task['id'],'local','operator',preview['id'],False,confirm_structure=True)
    run,_=service.create_export(task['id'],'local',2);service.generate(run['id'],'local')
    final=load_workbook(service.export_file(task['id'],run['id'],'local','标准商品表.xlsx'))
    assert len(final.active._images)==2
    assert final.active.max_column==6
    final.close()
    with ZipFile(service.export_file(task['id'],run['id'],'local','标准商品表.xlsx')) as z:
        images=[z.read(n) for n in z.namelist() if n.startswith('xl/media/')]
        assert sorted(hashlib.sha256(b).hexdigest() for b in images)==sorted(hashlib.sha256(png(c)).hexdigest() for c in ('red','blue'))
    with ZipFile(service.export_file(task['id'],run['id'],'local','最终表.zip')) as z:
        assert len([n for n in z.namelist() if n.startswith('原始图片/')])==3  # includes unplaced header logo
    assert (store.directory(task['id'])/'source.xlsx').read_bytes()==content
    store.engine.dispose()


@pytest.mark.parametrize('formula', ['=1/0','=SUM(A2:A2)','=WEBSERVICE("https://example.com")',"='[external.xlsx]Sheet1'!A1",'=UNKNOWN(1)'])
def test_unverifiable_formula_does_not_silently_use_cache(tmp_path,formula):
    book=Workbook();sheet=book.active;sheet.append(['商品名称','销售价']);sheet.append([formula,10]);path=tmp_path/'bad.xlsx';book.save(path);book.close()
    with pytest.raises(WorkflowError,match='无法校验'):
        discover(path,default_config().fields,supplier=True)


def test_broken_image_formula_has_actionable_error(tmp_path):
    book=Workbook();sheet=book.active;sheet.append(['商品名称','商品主图']);sheet.append(['台灯','=DISPIMG("missing",1)']);path=tmp_path/'missing.xlsx';book.save(path);book.close()
    with pytest.raises(WorkflowError,match='找不到对应的内嵌图片'):
        discover(path,default_config().fields,supplier=True)


@pytest.mark.parametrize('formula,expected',[('=-2^2','4'),('=2^3^2','64'),('=10-2-3','5'),
    ('=8/2*3','12'),('=100*15%','15'),('=ROUND(-1.25,1)','-1.3'),('=2*(3+4)','14')])
def test_excel_arithmetic_precedence_and_rounding(tmp_path,formula,expected):
    book=Workbook();sheet=book.active;sheet.append(['商品名称','销售价']);sheet.append(['台灯',formula]);path=tmp_path/'math.xlsx';book.save(path);book.close()
    sheets=discover(path,default_config().fields,supplier=True)
    assert sheets[0]['rows'][1][1]==expected


def test_repeated_images_share_media_without_losing_anchors(tmp_path):
    assets=tmp_path/'images';info=store_asset(assets,png())
    book=Workbook();sheet=book.active
    sheet['A1']=sheet['A2']='image://'+info['id']
    embed_images(sheet,assets,1)
    path=tmp_path/'pictures.xlsx';book.save(path);book.close();deduplicate_images(path)
    with ZipFile(path) as z:
        assert len([n for n in z.namelist() if n.startswith('xl/media/')])==1
    verified=load_workbook(path)
    assert len(verified.active._images)==2
    assert verified.active['A1'].number_format==';;;'
    verified.close()
