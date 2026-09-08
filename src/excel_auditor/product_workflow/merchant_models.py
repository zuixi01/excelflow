from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DiscoveredSheet:
    name: str
    header_row: int
    header_depth: int
    headers: list[str]
    last_row: int
    score: int


@dataclass
class MerchantRecord:
    record_id: str
    source_sheet: str
    source_row: int
    fixed: dict[str, Any]


@dataclass
class PlatformRecord:
    record_id: str
    fixed: dict[str, Any]
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProductMatch:
    merchant_record_id: str
    platform_record_id: str
    score: float
    tie_break_score: int = 0


@dataclass
class ReconciliationIssue:
    issue_type: str
    severity: str
    message: str
    source_sheet: str | None = None
    source_row: int | None = None
    field_id: str | None = None
    field_title: str | None = None
    merchant_value: Any = None
    platform_value: Any = None
    platform_record_id: str | None = None
    match_score: float | None = None


@dataclass
class OutputRecord:
    fixed: dict[str, Any]
    platform_extras: dict[str, Any]
    source_sheet: str | None
    source_row: int | None
    platform_record_id: str | None
    match_score: float | None
    difference_fields: set[str] = field(default_factory=set)
    row_status: str = "matched"


@dataclass
class ReconciliationResult:
    discovered_sheets: list[DiscoveredSheet]
    merchant_records: list[MerchantRecord]
    platform_records: list[PlatformRecord]
    matches: list[ProductMatch]
    output_records: list[OutputRecord]
    issues: list[ReconciliationIssue]
    platform_extension_fields: list[str]

    @property
    def difference_count(self) -> int:
        return sum(1 for issue in self.issues if issue.issue_type == "field_mismatch")

    @property
    def merchant_only_count(self) -> int:
        return sum(1 for row in self.output_records if row.row_status in {"merchant_only", "ambiguous"})

    @property
    def platform_only_count(self) -> int:
        return sum(1 for row in self.output_records if row.row_status == "platform_only")
