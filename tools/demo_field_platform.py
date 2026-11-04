"""Local platform fixture for the external-field workflow; never used by production."""
from pathlib import Path
import argparse

from fastapi import FastAPI

app = FastAPI(title="ExcelFlow field platform demo")


@app.get("/field-standards")
def fields():
    return {"data": {"schema_version": "demo-1.0", "fields": [
        {"id": "sku", "title": "商品编码", "aliases": ["货号"], "type": "string", "required": True},
        {"id": "name", "title": "商品名称", "aliases": ["品名"], "required": True},
        {"id": "price", "title": "销售价", "aliases": ["含税价格"], "type": "decimal", "required": True},
        {"id": "material", "title": "材质", "description": "商品的主要材料"},
    ]}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--generate", action="store_true")
    args = parser.parse_args()
    if args.generate:
        from openpyxl import Workbook
        destination = Path("var/field-demo")
        destination.mkdir(parents=True, exist_ok=True)
        book = Workbook()
        sheet = book.active
        sheet.title = "供应商报价"
        sheet.append(["货号", "品名", "含税价格", "用料"])
        sheet.append(["0001", "阅读台灯", "129.50", "铝合金"])
        sheet.append(["0002", "桌面收纳盒", "待确认", "木质"])
        sheet.append(["0003", "置物架", "79.00", "钢制"])
        book.save(destination / "supplier-demo.xlsx")
        book.close()
        print((destination / "supplier-demo.xlsx").resolve())
    else:
        import uvicorn
        uvicorn.run(app, host="127.0.0.1", port=8011)
