from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from .fixed_template import FIXED_FIELDS, match_fixed_field, normalize_text
from .merchant_models import PlatformRecord


class PlatformProductSource(Protocol):
    def load(self) -> tuple[list[PlatformRecord], list[str]]: ...


class EmptyPlatformProductSource:
    def load(self) -> tuple[list[PlatformRecord], list[str]]:
        return [], []


class FixturePlatformProductSource:
    """Development adapter for the product payload that a future platform API will return."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> tuple[list[PlatformRecord], list[str]]:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        rows = _records_from_payload(payload)
        records: list[PlatformRecord] = []
        extension_order: list[str] = []
        for index, raw in enumerate(rows, start=1):
            if not isinstance(raw, Mapping):
                raise ValueError(f"PLATFORM_DATA_INVALID: record {index} must be an object")
            fixed, extras = _parse_record(dict(raw))
            record_id = _platform_id(raw, index)
            if not normalize_text(fixed.get("product_name")):
                raise ValueError(f"PLATFORM_DATA_INVALID: record {index} is missing 商品名称")
            for title in extras:
                if title not in extension_order:
                    extension_order.append(title)
            records.append(PlatformRecord(record_id=record_id, fixed=fixed, extras=extras))
        return records, extension_order


def _records_from_payload(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ValueError("PLATFORM_DATA_INVALID: root must be an object or array")
    for key in ("records", "items", "data", "products"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for nested in ("records", "items", "list"):
                if isinstance(value.get(nested), list):
                    return value[nested]
    raise ValueError("PLATFORM_DATA_INVALID: product record array was not found")


def _parse_record(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    fixed = {field.field_id: None for field in FIXED_FIELDS}
    extras: dict[str, Any] = {}
    nested_fixed = _first_mapping(raw, "fixed", "fixed_fields", "fixedFields")
    nested_dynamic = _first_mapping(raw, "dynamic", "dynamic_fields", "dynamicFields", "extensions", "attributes")
    candidates = {**nested_fixed}
    candidates.update({
        key: value
        for key, value in raw.items()
        if key not in {
            "id", "platform_id", "platformId", "product_id", "productId",
            "fixed", "fixed_fields", "fixedFields", "dynamic", "dynamic_fields",
            "dynamicFields", "extensions", "attributes",
        }
    })
    fixed_ids = {field.field_id for field in FIXED_FIELDS}
    for key, value in candidates.items():
        field_id = str(key) if str(key) in fixed_ids else match_fixed_field(key)
        if field_id:
            fixed[field_id] = value
        elif normalize_text(key):
            extras[normalize_text(key)] = value
    for key, value in nested_dynamic.items():
        field_id = str(key) if str(key) in fixed_ids else match_fixed_field(key)
        if field_id:
            fixed[field_id] = value
        elif normalize_text(key):
            extras[normalize_text(key)] = value
    return fixed, extras


def _first_mapping(raw: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _platform_id(raw: Mapping[str, Any], index: int) -> str:
    for key in ("platform_id", "platformId", "product_id", "productId", "id"):
        value = raw.get(key)
        if value not in {None, ""}:
            return str(value)
    return f"platform-{index}"
