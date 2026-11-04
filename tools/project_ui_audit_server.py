"""Full application in an isolated directory, plus deterministic local platform fixtures."""
import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'outputs/project-audit-20261102'


def prepare():
    from openpyxl import Workbook
    OUTPUT.mkdir(parents=True, exist_ok=True)
    connections = {'connections': [{'id': 'audit-platform', 'base_url': 'http://127.0.0.1:8012/',
        'allowed_paths': ['/field-standards'], 'allow_private_network': True, 'allow_insecure_http': True,
        'field_reconciliation': {'title': '隔离验收平台', 'path': '/field-standards', 'tenant_ids': ['local'], 'parameter_mapping': {}}}]}
    (OUTPUT / 'connections.json').write_text(json.dumps(connections), 'utf-8')
    book = Workbook(); sheet = book.active; sheet.title = '供应商报价'
    sheet.append(['货号', '品名', '含税价格', '用料'])
    for i in range(35):
        sheet.append([f'{i:04}', f'台灯{i}', '待确认' if i < 32 else '129.50', '铝合金'])
    book.save(OUTPUT / '字段核对测试.xlsx'); book.close()
    book = Workbook(); sheet = book.active; sheet.title = '商品'
    sheet.append(['商品名称', '货号', '售价', '品牌', '功率', '备注'])
    for i in range(45):
        sheet.append([f'台灯{i}', f'{i:04}', 10+i, '测试品牌', '3W', f'原文{i}'])
    book.save(OUTPUT / '商品测试.xlsx'); book.close()


def platform():
    from fastapi import FastAPI
    app = FastAPI()
    @app.get('/field-standards')
    def fields():
        return {'data': {'schema_version': 'audit-1', 'fields': [
            {'id':'sku','title':'商品编码','aliases':['货号'],'required':True},
            {'id':'name','title':'商品名称','aliases':['品名'],'required':True},
            {'id':'price','title':'销售价','aliases':['含税价格'],'type':'decimal','required':True},
            {'id':'material','title':'材质'},
        ]}}
    @app.get('/v1/models')
    def models():
        return {'data': [{'id':'audit-model'}]}
    @app.post('/v1/chat/completions')
    def completions():
        return {'choices':[{'message':{'content':'{"mappings":[]}'}}]}
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('operation', choices=['prepare','api','platform'])
    operation = parser.parse_args().operation
    if operation == 'prepare':
        prepare()
    else:
        import uvicorn
        if operation == 'api':
            os.environ.update(EXCEL_AUDITOR_DATA=str(OUTPUT / '.runtime'), EXCEL_AUDITOR_REQUIRE_AUTH='1',
                EXCEL_AUDITOR_ADMIN_USERNAME='audit-admin', EXCEL_AUDITOR_ADMIN_PASSWORD='audit-test-password',
                EXCEL_AUDITOR_ENV='development', EXCEL_AUDITOR_ALLOW_INSECURE_AI_HTTP='1',
                EXCEL_AUDITOR_CONNECTIONS=str(OUTPUT / 'connections.json'))
            for name in ('DATABASE_URL','REDIS_URL','S3_BUCKET','EXCEL_AUDITOR_AUTH_DATABASE_URL','EXCEL_AUDITOR_API_TOKEN','EXCEL_AUDITOR_API_TOKENS_JSON'):
                os.environ.pop(name, None)
            from excel_auditor.api import app
            uvicorn.run(app, host='127.0.0.1', port=8004, log_level='warning')
        else:
            uvicorn.run(platform(), host='127.0.0.1', port=8012, log_level='warning')
