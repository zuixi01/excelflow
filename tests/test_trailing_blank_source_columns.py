from io import BytesIO

from openpyxl import Workbook
from openpyxl.drawing.image import Image
from openpyxl.styles import PatternFill
from PIL import Image as PillowImage

from excel_auditor.workflow.excel import _content_bounds, discover
from excel_auditor.workflow.models import MappingRequest, starter_config
from excel_auditor.workflow.retained_columns import retain_source_columns


def test_whitespace_tail_removed_but_interior_columns_and_images_preserved(tmp_path):
    book=Workbook();sheet=book.active
    sheet.append(['品类','型号',None,None,'图片','规格','备注1','备注2'])
    sheet.append(['坐便器','A8',None,None,None,'规格说明','含税','安装'])
    sheet['M5']=' ';sheet['M6']='\t\u3000\u00a0'
    sheet['O8'].fill=PatternFill('solid',fgColor='FFFF00')
    photo=BytesIO();PillowImage.new('RGB',(8,8),'blue').save(photo,format='PNG');photo.seek(0)
    sheet.add_image(Image(photo),'E2')
    path=tmp_path/'source.xlsx';book.save(path);book.close()
    source=discover(path,[],supplier=True,preserve_extras=True,faithful=True)[0]
    assert len(source['columns'])==8
    assert len(source['rows'])==2
    assert source['rows'][1][4].startswith('image://')
    assert source['columns'][2]['column']==3 and source['columns'][3]['column']==4
    mapping=MappingRequest(base_revision=0,sheets=[{'sheet':source['sheet'],'header_row':1,'header_depth':1,'include':True,'bindings':[]}])
    config=retain_source_columns(starter_config(),[source],mapping.sheets)
    assert [field.source_column.column for field in config.fields if field.source_column]==list(range(1,9))


def test_named_empty_column_zero_and_false_are_content():
    book=Workbook();sheet=book.active
    sheet['A1']='商品名称';sheet['H1']='预留属性';sheet['K2']=0;sheet['L3']=False
    sheet['M4']='\n '
    assert _content_bounds(sheet)==(3,12)
    sheet['L3']=None;sheet['K2']=None
    assert _content_bounds(sheet)==(1,8)
    book.close()


def test_old_snapshot_trims_only_unnamed_empty_tail():
    source={'sheet':'原表','columns':[{'column':i} for i in range(1,7)],
            'rows':[['商品名称','','图片','预留属性','',''],['商品','','image://'+'a'*64,'',' ','\t']]}
    mapping=MappingRequest(base_revision=0,sheets=[{'sheet':'原表','include':True,'header_row':1,'header_depth':1,'bindings':[]}])
    config=retain_source_columns(starter_config(),[source],mapping.sheets)
    assert [field.source_column.column for field in config.fields if field.source_column]==[1,2,3,4]
