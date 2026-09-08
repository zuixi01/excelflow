from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .fixed_template import BOOLEAN_FIELDS, DATE_FIELDS, FIXED_FIELDS, NUMERIC_FIELDS, normalize_text
from .merchant_models import (
    DiscoveredSheet,
    MerchantRecord,
    OutputRecord,
    PlatformRecord,
    ReconciliationIssue,
    ReconciliationResult,
)
from .product_matcher import match_products


def reconcile_products(
    discovered_sheets: list[DiscoveredSheet],
    merchant_records: list[MerchantRecord],
    platform_records: list[PlatformRecord],
    platform_extension_fields: list[str],
    *,
    threshold: float = 90,
    platform_available: bool = True,
) -> ReconciliationResult:
    if not platform_available:
        return ReconciliationResult(
            discovered_sheets=discovered_sheets,
            merchant_records=merchant_records,
            platform_records=[],
            matches=[],
            output_records=[
                OutputRecord(
                    fixed=dict(record.fixed),
                    platform_extras={},
                    source_sheet=record.source_sheet,
                    source_row=record.source_row,
                    platform_record_id=None,
                    match_score=None,
                    row_status="source_only",
                )
                for record in merchant_records
            ],
            issues=[],
            platform_extension_fields=[],
        )
    matches, issues, ambiguous = match_products(merchant_records, platform_records, threshold=threshold)
    match_by_merchant = {match.merchant_record_id: match for match in matches}
    platform_by_id = {record.record_id: record for record in platform_records}
    matched_platform_ids = {match.platform_record_id for match in matches}
    output: list[OutputRecord] = []

    for merchant in merchant_records:
        match = match_by_merchant.get(merchant.record_id)
        if match is None:
            status = "ambiguous" if merchant.record_id in ambiguous else "merchant_only"
            output.append(OutputRecord(
                fixed=dict(merchant.fixed),
                platform_extras={},
                source_sheet=merchant.source_sheet,
                source_row=merchant.source_row,
                platform_record_id=None,
                match_score=None,
                difference_fields=set(),
                row_status=status,
            ))
            if status == "merchant_only":
                issues.append(ReconciliationIssue(
                    issue_type="merchant_only_product",
                    severity="warning",
                    source_sheet=merchant.source_sheet,
                    source_row=merchant.source_row,
                    field_id="product_name",
                    field_title="商品名称",
                    merchant_value=merchant.fixed.get("product_name"),
                    message="未找到相似度达到阈值的平台商品，商家商品已保留。",
                ))
            continue

        platform = platform_by_id[match.platform_record_id]
        fixed, differences, row_issues = _merge_fixed_fields(merchant, platform, match.score)
        issues.extend(row_issues)
        output.append(OutputRecord(
            fixed=fixed,
            platform_extras=dict(platform.extras),
            source_sheet=merchant.source_sheet,
            source_row=merchant.source_row,
            platform_record_id=platform.record_id,
            match_score=match.score,
            difference_fields=differences,
            row_status="matched",
        ))

    for platform in platform_records:
        if platform.record_id in matched_platform_ids:
            continue
        output.append(OutputRecord(
            fixed=dict(platform.fixed),
            platform_extras=dict(platform.extras),
            source_sheet=None,
            source_row=None,
            platform_record_id=platform.record_id,
            match_score=None,
            row_status="platform_only",
        ))
        issues.append(ReconciliationIssue(
            issue_type="platform_only_product",
            severity="info",
            field_id="product_name",
            field_title="商品名称",
            platform_value=platform.fixed.get("product_name"),
            platform_record_id=platform.record_id,
            message="平台商品在商家工作表中不存在，已追加到汇总表底部。",
        ))

    return ReconciliationResult(
        discovered_sheets=discovered_sheets,
        merchant_records=merchant_records,
        platform_records=platform_records,
        matches=matches,
        output_records=output,
        issues=issues,
        platform_extension_fields=platform_extension_fields,
    )


def values_equal(field_id: str, left: Any, right: Any) -> bool:
    if _missing(left) and _missing(right):
        return True
    if _missing(left) or _missing(right):
        return False
    if field_id in NUMERIC_FIELDS:
        left_number = _decimal_value(left)
        right_number = _decimal_value(right)
        if left_number is not None and right_number is not None:
            return left_number == right_number
    if field_id in BOOLEAN_FIELDS:
        left_boolean = _boolean_value(left)
        right_boolean = _boolean_value(right)
        if left_boolean is not None and right_boolean is not None:
            return left_boolean == right_boolean
    if field_id in DATE_FIELDS:
        return _date_value(left) == _date_value(right)
    return normalize_text(left).casefold() == normalize_text(right).casefold()


def _merge_fixed_fields(
    merchant: MerchantRecord,
    platform: PlatformRecord,
    match_score: float,
) -> tuple[dict[str, Any], set[str], list[ReconciliationIssue]]:
    merged: dict[str, Any] = {}
    differences: set[str] = set()
    issues: list[ReconciliationIssue] = []
    for field in FIXED_FIELDS:
        merchant_value = merchant.fixed.get(field.field_id)
        platform_value = platform.fixed.get(field.field_id)
        if _missing(merchant_value) and not _missing(platform_value):
            merged[field.field_id] = platform_value
            continue
        merged[field.field_id] = merchant_value
        # Reaching this point means the product-name fuzzy rule already decided
        # these two rows represent the same product.
        if field.field_id == "product_name":
            continue
        if _missing(platform_value) or values_equal(field.field_id, merchant_value, platform_value):
            continue
        differences.add(field.field_id)
        issues.append(ReconciliationIssue(
            issue_type="field_mismatch",
            severity="error",
            source_sheet=merchant.source_sheet,
            source_row=merchant.source_row,
            field_id=field.field_id,
            field_title=field.title if field.group is None else f"{field.group}/{field.title}",
            merchant_value=merchant_value,
            platform_value=platform_value,
            platform_record_id=platform.record_id,
            match_score=match_score,
            message=f"{field.title}与平台值不同，汇总表保留商家值并标红。",
        ))
    return merged, differences, issues


def _missing(value: Any) -> bool:
    return value is None or isinstance(value, str) and not normalize_text(value)


def _decimal_value(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    text = normalize_text(value).replace(",", "")
    match = re.fullmatch(r"([-+]?\d+(?:\.\d+)?)(?:元|天|件|个|套|片)?", text)
    if not match:
        return None
    try:
        return Decimal(match.group(1))
    except InvalidOperation:
        return None


def _boolean_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    token = normalize_text(value).casefold()
    if token in {"是", "有", "包含", "true", "1", "yes", "y"}:
        return True
    if token in {"否", "无", "不包含", "false", "0", "no", "n"}:
        return False
    return None


def _date_value(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return normalize_text(value).casefold()
