"""Loopback-only acceptance server with disposable Excel fixtures, never formal data."""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
os.environ['EXCEL_AUDITOR_ENV'] = 'development'
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from openpyxl import load_workbook
from excel_auditor.workflow.models import WorkflowError
from excel_auditor.workflow.routes import build_router
from test_flexible_maintenance_return import runtime, supplied

out = ROOT / 'outputs/simple-return-20261103'
out.mkdir(parents=True, exist_ok=True)
data_root = Path(tempfile.mkdtemp(prefix='runtime-', dir=out))
fixture = runtime.__wrapped__(data_root)
store, service, task = next(fixture)
with store.engine.begin() as con:
    owned = store.get(con, task['id'], 'local', 'task')
    owned['payload']['actor'] = 'local'
    store.save(con, owned)
book = load_workbook(service.maintenance_file(task['id'], 'local'))
sheet = book['维护表']
sheet['A4'] = None
uncertain_file = out / '测试回传_ID缺失待确认.xlsx'
uncertain_file.write_bytes(supplied(book))
sheet['A4'] = 'row_0'
sheet['D3'] = '厂牌'
first, second = [c.value for c in sheet[4]], [c.value for c in sheet[5]]
for index, values in enumerate([second, first], 4):
    for column, value in enumerate(values, 1):
        sheet.cell(index, column).value = value
sheet['C5'] = '21'
sheet['A6'], sheet['B6'], sheet['F6'] = None, None, '新增无货号商品'
file = out / '测试回传_改名改价新增删除排序.xlsx'
file.write_bytes(supplied(book))
book.close()
(out / 'fixture.json').write_text(json.dumps({'task_id': task['id'], 'file': str(file), 'uncertain_file': str(uncertain_file), 'runtime': str(data_root)}, ensure_ascii=False), 'utf-8')
app = FastAPI()
@app.middleware('http')
async def identity(request: Request, call_next):
    request.state.tenant_id = request.state.user_id = 'local'
    return await call_next(request)
@app.exception_handler(WorkflowError)
async def error(request, exc):
    return JSONResponse({'detail': str(exc), 'code': exc.code}, status_code=exc.status)
app.include_router(build_router(data_root))

if __name__ == '__main__':
    import uvicorn
    try:
        uvicorn.run(app, host='127.0.0.1', port=8017)
    finally:
        fixture.close()
