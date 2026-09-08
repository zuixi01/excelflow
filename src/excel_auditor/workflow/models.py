from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, model_validator

from ..models import ColumnRule, StrictModel, normalize_header as normalize_rule_header, _formula_code_outside_string_literals, _FORBIDDEN_FORMULA_CODE
from ..product_workflow.fixed_template import FIXED_FIELDS


class WorkflowError(ValueError):
    def __init__(self, message: str, status: int = 422, code: str = "WORKFLOW_INVALID"):
        super().__init__(message)
        self.status = status
        self.code = code


class MaintenanceField(StrictModel):
    rule: ColumnRule
    editable: bool = True
    default: str | None = None


class OutputColumn(StrictModel):
    title: str = Field(min_length=1, max_length=255)
    field: str = ""
    operation: Literal["copy", "constant", "multiply", "concat", "split", "formula"] = "copy"
    argument: str = Field(default="", max_length=1024)
    sources: list[str] = Field(default_factory=list, max_length=20)
    part: int = Field(default=0, ge=0, le=50)
    required: bool = False
    number_format: str = Field(default="", max_length=255)
    width: float = Field(default=18, ge=4, le=80)

    @model_validator(mode="after")
    def safe_operation(self):
        if self.operation == "multiply":
            try:
                factor = Decimal(self.argument)
                if not factor.is_finite() or len(self.argument) > 40:
                    raise ValueError
            except Exception as exc:
                raise ValueError("换算系数必须是有限数值") from exc
        if self.operation == "split" and not self.argument:
            raise ValueError("拆分规则需要分隔符")
        if self.operation == "formula":
            formula = self.argument.replace("{row}", "2")
            code = _formula_code_outside_string_literals(formula)
            if (not formula.startswith("=") or code is None or _FORBIDDEN_FORMULA_CODE.search(code)
                    or "{" in formula or "}" in formula or "!" in code or ";" in code):
                raise ValueError("仅支持本表的安全公式，可用 {row} 表示数据行；不允许外部引用")
        if any(ord(char) < 32 for char in self.title):
            raise ValueError("输出表头不能包含控制字符")
        return self


class OutputSheet(StrictModel):
    name: str = Field(default="商品导入", min_length=1, max_length=31)
    header_row: int = Field(default=1, ge=1, le=50)
    data_start_row: int = Field(default=2, ge=2, le=100)
    columns: list[OutputColumn] = Field(min_length=1, max_length=200)
    group_by: str = ""
    sku_fields: list[str] = Field(default_factory=list, max_length=5)
    sku_separator: str = Field(default="|", min_length=1, max_length=5)

    @model_validator(mode="after")
    def layout(self):
        if re.search(r"[\\/*?:\[\]]", self.name) or self.name.startswith("_"):
            raise ValueError("工作表名包含非法字符或使用了保留前缀")
        if self.data_start_row <= self.header_row:
            raise ValueError("数据起始行必须在表头之后")
        if len({column.title for column in self.columns}) != len(self.columns):
            raise ValueError("输出字段名称不能重复")
        return self


class OutputFile(StrictModel):
    name: str = Field(default="商品导入.xlsx", min_length=6, max_length=100)
    sheets: list[OutputSheet] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def file_name(self):
        if not self.name.endswith(".xlsx") or re.search(r'[\\/:*?"<>|\x00-\x1f]', self.name) or self.name.startswith("."):
            raise ValueError("文件名必须是普通 .xlsx 文件名")
        if len({s.name.casefold() for s in self.sheets}) != len(self.sheets):
            raise ValueError("同一文件中的工作表名称不能重复")
        return self


class CrossCheck(StrictModel):
    kind: Literal["less_equal", "conditional_required"]
    left: str
    right: str
    equals: str = ""
    message: str = Field(default="字段之间不符合业务规则", max_length=300)


class ImportRule(StrictModel):
    field: str
    headers: list[str] = Field(default_factory=list, max_length=20)
    operation: Literal["copy", "trim", "multiply", "concat", "split", "constant"] = "copy"
    argument: str = Field(default="", max_length=1024)
    part: int = Field(default=0, ge=0, le=50)


class WorkflowConfig(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    fields: list[MaintenanceField] = Field(min_length=1, max_length=200)
    import_rules: list[ImportRule] = Field(default_factory=list, max_length=200)
    unique_key: list[str] = Field(default_factory=list, max_length=10)
    cross_checks: list[CrossCheck] = Field(default_factory=list, max_length=30)
    outputs: list[OutputFile] = Field(min_length=1, max_length=10)
    preserve_extras: bool = True

    @model_validator(mode="after")
    def references(self):
        ids = [item.rule.name for item in self.fields]
        if len(set(ids)) != len(ids) or len({item.rule.title for item in self.fields}) != len(ids):
            raise ValueError("维护字段 ID 和显示名必须唯一")
        if any(name.startswith("_workflow") for name in ids):
            raise ValueError("维护字段使用了系统保留名称")
        known = set(ids)
        if len({rule.field for rule in self.import_rules}) != len(self.import_rules):
            raise ValueError("每个维护字段只能配置一条导入转换规则")
        for rule in self.import_rules:
            if rule.field not in known:
                raise ValueError("导入规则引用了不存在的字段")
            if rule.operation != "constant" and not rule.headers:
                raise ValueError("导入转换需要来源表头")
            if rule.operation in {"copy", "trim", "multiply", "split"} and len(rule.headers) != 1:
                raise ValueError("此导入转换需要一个来源表头")
            if rule.operation in {"multiply", "split"}:
                OutputColumn(title="import", operation=rule.operation, argument=rule.argument)
        if set(self.unique_key) - known:
            raise ValueError("唯一键引用了不存在的字段")
        field_types = {field.rule.name: field.rule.type.value for field in self.fields}
        for check in self.cross_checks:
            if {check.left, check.right} - known:
                raise ValueError("跨字段规则引用了不存在的字段")
            if check.kind == "less_equal" and (field_types[check.left] != field_types[check.right]
                    or field_types[check.left] not in {"integer", "decimal", "date", "datetime"}):
                raise ValueError("大小比较需要相同的数值或日期类型")
        if len({file.name.casefold() for file in self.outputs}) != len(self.outputs):
            raise ValueError("输出文件名不能重复")
        for file in self.outputs:
            for sheet in file.sheets:
                refs = {sheet.group_by, *sheet.sku_fields}
                for column in sheet.columns:
                    refs.update(column.sources)
                    if column.operation not in {"constant", "formula"}:
                        refs.add(column.field)
                        if not column.field and column.operation != "concat":
                            raise ValueError(f"输出字段 {column.title} 尚未关联维护字段")
                    if column.operation == "concat" and not column.sources:
                        raise ValueError("拼接规则至少需要一个源字段")
                if refs - known - {""}:
                    raise ValueError(f"输出模板引用了不存在的字段：{', '.join(sorted(refs - known - {''}))}")
        # Defaults must satisfy the same parser as maintenance records.
        from ..normalization import parse_value
        from ..engine import _validate
        for field in self.fields:
            if field.default is not None:
                parsed = parse_value(field.default, field.rule)
                if not parsed.valid or _validate(parsed, field.rule):
                    raise ValueError(f"{field.rule.title} 的默认值不符合字段规则")
        return self


class Binding(StrictModel):
    field: str
    columns: list[int] = Field(default_factory=list, max_length=20)
    operation: Literal["copy", "trim", "multiply", "concat", "split", "constant"] = "copy"
    argument: str = Field(default="", max_length=1024)
    part: int = Field(default=0, ge=0, le=50)

    @model_validator(mode="after")
    def valid_columns(self):
        if any(column < 1 or column > 200 for column in self.columns):
            raise ValueError("源列编号越界")
        if self.operation != "constant" and not self.columns:
            raise ValueError("请为字段选择来源列")
        if self.operation in {"copy", "trim", "multiply", "split"} and len(self.columns) != 1:
            raise ValueError("此转换只能选择一个来源列")
        if self.operation == "multiply":
            OutputColumn(title="factor", operation="multiply", argument=self.argument)
        if self.operation == "split" and not self.argument:
            raise ValueError("拆分需要分隔符")
        return self


class SheetMapping(StrictModel):
    sheet: str
    header_row: int = Field(ge=1, le=50)
    header_depth: int = Field(default=1, ge=1, le=3)
    include: bool = True
    bindings: list[Binding] = Field(default_factory=list, max_length=200)
    ignored_columns: list[int] = Field(default_factory=list, max_length=200)


class MappingRequest(StrictModel):
    base_revision: int = Field(ge=0)
    sheets: list[SheetMapping] = Field(min_length=1, max_length=50)


class EditRequest(StrictModel):
    base_revision: int = Field(ge=1)
    changes: dict[str, dict[str, str | None]] = Field(default_factory=dict, max_length=2000)
    additions: list[dict[str, str | None]] = Field(default_factory=list, max_length=1000)
    deletions: list[str] = Field(default_factory=list, max_length=1000)


class RevisionRequest(StrictModel):
    base_revision: int = Field(ge=0)


class TemplateEdit(StrictModel):
    base_revision: int = Field(ge=0)
    config: WorkflowConfig
    confirmed: bool = False


def content_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def default_config() -> WorkflowConfig:
    fields = []
    for item in FIXED_FIELDS:
        title = f"{item.group}/{item.title}" if item.group else item.title
        field_type = "string" if item.value_type == "text" else item.value_type
        fields.append(MaintenanceField(rule=ColumnRule(
            name=item.field_id, title=title, aliases=list({normalize_rule_header(alias): alias for alias in item.aliases}.values()),
            type=field_type, required=item.field_id == "product_name",
            normalize=["trim"] if field_type != "boolean" else [],
        )))
    return WorkflowConfig(
        name="商家商品上架整理", description="合并商家商品表，维护标准字段，生成商品导入文件。",
        fields=fields, outputs=[OutputFile(sheets=[OutputSheet(columns=[
            OutputColumn(title=field.rule.title, field=field.rule.name, required=field.rule.required)
            for field in fields
        ])])],
    )
