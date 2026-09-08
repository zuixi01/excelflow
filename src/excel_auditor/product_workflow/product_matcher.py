from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from rapidfuzz import fuzz

from .fixed_template import normalize_text
from .merchant_models import MerchantRecord, PlatformRecord, ProductMatch, ReconciliationIssue


_TIE_BREAK_WEIGHTS = {"sku": 8, "brand": 4, "product_category": 2, "sales_unit": 1}


def normalize_product_name(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", normalize_text(value).casefold())


def match_products(
    merchant_records: list[MerchantRecord],
    platform_records: list[PlatformRecord],
    *,
    threshold: float = 90,
) -> tuple[list[ProductMatch], list[ReconciliationIssue], set[str]]:
    if not 0 <= threshold <= 100:
        raise ValueError("name_match_threshold must be between 0 and 100")
    candidates: dict[str, list[tuple[float, int, str]]] = defaultdict(list)
    for merchant in merchant_records:
        merchant_name = normalize_product_name(merchant.fixed.get("product_name"))
        for platform in platform_records:
            platform_name = normalize_product_name(platform.fixed.get("product_name"))
            score = 100.0 if merchant_name == platform_name else float(fuzz.WRatio(merchant_name, platform_name))
            if score >= threshold:
                candidates[merchant.record_id].append((score, _tie_break_score(merchant, platform), platform.record_id))
        candidates[merchant.record_id].sort(key=lambda item: (-item[0], -item[1], item[2]))

    merchant_order = sorted(
        merchant_records,
        key=lambda record: (
            -candidates[record.record_id][0][0] if candidates[record.record_id] else 1,
            -candidates[record.record_id][0][1] if candidates[record.record_id] else 0,
            record.record_id,
        ),
    )
    used_platform: set[str] = set()
    matches: list[ProductMatch] = []
    issues: list[ReconciliationIssue] = []
    ambiguous: set[str] = set()
    for merchant in merchant_order:
        available = [item for item in candidates[merchant.record_id] if item[2] not in used_platform]
        if not available:
            continue
        best_score, best_tie, best_platform_id = available[0]
        equally_best = [item for item in available if item[:2] == (best_score, best_tie)]
        if len(equally_best) > 1:
            ambiguous.add(merchant.record_id)
            issues.append(ReconciliationIssue(
                issue_type="ambiguous_product_match",
                severity="warning",
                source_sheet=merchant.source_sheet,
                source_row=merchant.source_row,
                field_id="product_name",
                field_title="商品名称",
                merchant_value=merchant.fixed.get("product_name"),
                match_score=best_score,
                message="多个平台商品的名称相似度和辅助字段得分相同，未自动合并。",
            ))
            continue
        used_platform.add(best_platform_id)
        matches.append(ProductMatch(
            merchant_record_id=merchant.record_id,
            platform_record_id=best_platform_id,
            score=best_score,
            tie_break_score=best_tie,
        ))
    return matches, issues, ambiguous


def _tie_break_score(merchant: MerchantRecord, platform: PlatformRecord) -> int:
    score = 0
    for field_id, weight in _TIE_BREAK_WEIGHTS.items():
        merchant_value = normalize_text(merchant.fixed.get(field_id)).casefold()
        platform_value = normalize_text(platform.fixed.get(field_id)).casefold()
        if merchant_value and merchant_value == platform_value:
            score += weight
    return score
