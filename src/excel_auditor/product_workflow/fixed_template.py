from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FixedField:
    field_id: str
    title: str
    aliases: tuple[str, ...] = ()
    group: str | None = None
    value_type: str = "text"


FIXED_FIELDS: tuple[FixedField, ...] = (
    FixedField("product_category", "商品分类"),
    FixedField("is_bundle", "是否组合商品", ("是否组合", "组合商品"), value_type="boolean"),
    FixedField("product_name", "商品名称", ("产品名称", "品名", "名称")),
    FixedField("sales_unit", "销售单位", ("计价单位", "基本单位", "单位")),
    FixedField("quantity_mode", "数量取值", ("数量方式", "数量取值方式")),
    FixedField("brand", "商品品牌", ("产品品牌", "品牌")),
    FixedField("main_image", "商品主图", ("主图", "商品图片", "元素(图片)", "元素（图片）")),
    FixedField("detail_images", "商品详情图", ("详情图", "详情图片", "商品详情图片")),
    FixedField("sku", "货号", ("产品型号", "商品型号", "型号", "商品货号", "内部货号")),
    FixedField("spec_image", "规格图片", ("规格图", "SKU图片", "sku图片")),
    FixedField("market_price", "市场价", ("市场价线上价4", "市场价线上价", "市场价格"), value_type="decimal"),
    FixedField("sale_price", "销售价", ("建议平台卖价", "平台卖价", "售价", "价格"), value_type="decimal"),
    FixedField("stock", "库存", ("库存量", "可售库存"), value_type="decimal"),
    FixedField("handling_fee_template", "搬运费模板", ("搬运模板",)),
    FixedField("package_quantity", "数值", ("包装量数值", "包装数值"), group="包装量", value_type="decimal"),
    FixedField("package_unit", "单位", ("包装量单位", "包装单位"), group="包装量"),
    FixedField("return_days", "退货天数", ("退换货天数",), group="服务保障", value_type="decimal"),
    FixedField("service_description", "服务说明", ("服务保障说明",), group="服务保障"),
    FixedField("warranty_days", "质保天数", ("保修天数", "质保期"), group="售后保障", value_type="decimal"),
    FixedField("after_sales_description", "售后说明", ("售后保障说明",), group="售后保障"),
    FixedField("delivery_method", "配送方式", ("物流方式",), group="物流配送"),
    FixedField("freight_template", "运费模板", ("物流模板",), group="物流配送"),
    FixedField(
        "home_entry_fee_included",
        "入户费是否包含在商品价格中",
        ("入户费是否包含", "入户费包含在商品价格中"),
        group="物流配送",
        value_type="boolean",
    ),
    FixedField("other_description", "其他说明", ("其他情况备注", "其他备注", "备注"), group="物流配送"),
    FixedField("listed_at", "上架时间", ("上架日期",), value_type="datetime"),
)

FIXED_BY_ID = {field.field_id: field for field in FIXED_FIELDS}
NUMERIC_FIELDS = {field.field_id for field in FIXED_FIELDS if field.value_type == "decimal"}
BOOLEAN_FIELDS = {field.field_id for field in FIXED_FIELDS if field.value_type == "boolean"}
DATE_FIELDS = {field.field_id for field in FIXED_FIELDS if field.value_type == "datetime"}


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).replace("\u3000", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_header(value: Any) -> str:
    text = normalize_text(value).casefold()
    return re.sub(r"[\s/\\|:：()（）\[\]【】_-]+", "", text)


def field_tokens(field: FixedField) -> set[str]:
    values = {field.title, *field.aliases}
    if field.group:
        values.update({f"{field.group}{field.title}", f"{field.group}/{field.title}"})
    return {normalize_header(value) for value in values if normalize_header(value)}


FIELD_TOKEN_OWNERS: dict[str, set[str]] = {}
for fixed_field in FIXED_FIELDS:
    for token in field_tokens(fixed_field):
        FIELD_TOKEN_OWNERS.setdefault(token, set()).add(fixed_field.field_id)


def match_fixed_field(header: Any) -> str | None:
    """Return an unambiguous fixed-field id for a physical or flattened header."""
    parts = [part for part in re.split(r"\s*/\s*", normalize_text(header)) if part]
    candidates = [normalize_header(header), *(normalize_header(part) for part in reversed(parts))]
    for token in candidates:
        owners = FIELD_TOKEN_OWNERS.get(token, set())
        if len(owners) == 1:
            return next(iter(owners))
    return None


def fixed_titles() -> list[str]:
    return [field.title for field in FIXED_FIELDS]
