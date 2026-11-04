from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import select

from ..models import ColumnRule, StrictModel, normalize_header
from .models import MaintenanceField, OutputColumn, OutputFile, OutputSheet, WorkflowConfig, WorkflowError, content_hash
from .repository import WorkflowStore, objects


CATEGORY_FIELD = "source_category"


class ConfiguredField(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    title: str = Field(min_length=1, max_length=100)
    group: str = Field(default="", max_length=80)
    description: str = Field(default="", max_length=1000)
    aliases: list[str] = Field(default_factory=list, max_length=30)
    type: Literal["string", "id_code", "integer", "decimal", "boolean", "date", "datetime", "enum"] = "string"
    required: bool = False
    editable: bool = True
    enabled: bool = True
    default: str | None = Field(default=None, max_length=1000)
    enum_values: list[str] = Field(default_factory=list, max_length=500)
    min: str | None = None
    max: str | None = None

    @model_validator(mode="after")
    def valid(self):
        self.title = self.title.strip()
        self.group = self.group.strip()
        if not self.title or "/" in self.title or "/" in self.group or any(ord(c) < 32 for c in self.title + self.group):
            raise ValueError("字段名称和分组不能为空或包含斜杠、控制字符")
        if self.id == CATEGORY_FIELD or self.id.startswith("local_"):
            raise ValueError("字段 ID 使用了保留名称")
        if self.type == "enum" and not self.enum_values:
            raise ValueError("选项字段需要至少一个可选值")
        return self


class LocalCategory(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    title: str = Field(min_length=1, max_length=80)
    attributes: list[ConfiguredField] = Field(default_factory=list, max_length=100)
    specifications: list[ConfiguredField] = Field(default_factory=list, max_length=100)


class PlatformSource(StrictModel):
    connection_id: str = Field(min_length=1, max_length=128)
    schema_version: str = Field(default='', max_length=100)
    fetched_at: str = Field(max_length=50)


class FieldProfile(StrictModel):
    name: str = Field(default="商品标准化", min_length=1, max_length=80)
    fixed_fields: list[ConfiguredField] = Field(min_length=1, max_length=150)
    local_categories: list[LocalCategory] = Field(default_factory=list, max_length=19)
    platform_source: PlatformSource | None = None

    @model_validator(mode="after")
    def valid(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("模板名称不能为空")
        for items in [self.fixed_fields, *(c.attributes for c in self.local_categories), *(c.specifications for c in self.local_categories)]:
            if len({f.id for f in items}) != len(items) or len({(f.group, f.title) for f in items}) != len(items):
                raise ValueError("同组字段 ID 和名称不能重复")
        if len({c.id for c in self.local_categories}) != len(self.local_categories) or len({c.title for c in self.local_categories}) != len(self.local_categories):
            raise ValueError("类目 ID 和名称不能重复")
        if any(c.id == "unclassified" or not c.title.strip() or "/" in c.title for c in self.local_categories):
            raise ValueError("类目名称无效或 ID 使用了保留名称")
        if not any(f.enabled for f in self.fixed_fields):
            raise ValueError("至少启用一个固定字段")
        compile_profile(self)
        return self


class ProfileEdit(StrictModel):
    base_revision: int = Field(ge=0)
    profile: FieldProfile


class NewFieldProfile(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    source_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def valid(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("模板名称不能为空")
        return self


def initial_profile() -> FieldProfile:
    from ..product_workflow.fixed_template import FIXED_FIELDS
    return FieldProfile(fixed_fields=[ConfiguredField(
        id=f.field_id, title=f.title, group=f.group or "", aliases=list(f.aliases),
        type="id_code" if f.field_id == "sku" else "string" if f.value_type == "text" else f.value_type,
        required=f.field_id == "product_name",
    ) for f in FIXED_FIELDS])


def dynamic_id(category: LocalCategory, kind: str, field: ConfiguredField) -> str:
    return f"local.{category.id}.{kind}.{field.id}"


def compile_profile(profile: FieldProfile) -> WorkflowConfig:
    """Compile all configured fields into one target structure, without worksheet gating."""
    fields, columns = [], []

    def add(field, field_id, title):
        rule = ColumnRule(name=field_id, title=title, type=field.type, required=field.required,
                          aliases=list({normalize_header(a): a for a in [field.title, *field.aliases]}.values()),
                          enum_values=field.enum_values, normalize=["trim"], validation={"min": field.min, "max": field.max})
        fields.append(MaintenanceField(rule=rule, editable=field.editable, default=field.default, description=field.description))
        columns.append(OutputColumn(title=title, field=field_id, required=field.required))

    for field in profile.fixed_fields:
        if field.enabled:
            add(field, field.id, "/".join(filter(None, ["固定字段", field.group, field.title])))
    # Groups identify fields, never filter which supplier worksheets can use them.
    for kind, label, member in [("attribute", "商品属性", "attributes"), ("specification", "商品规格", "specifications")]:
        for category in profile.local_categories:
            for field in getattr(category, member):
                if field.enabled:
                    group = category.title if len(profile.local_categories) > 1 else ""
                    title = "/".join(filter(None, ["动态字段", label, group, field.title]))
                    add(field, dynamic_id(category, kind, field), title)
    depth = max(len(column.title.split('/')) for column in columns)
    sheet = OutputSheet(name="标准商品", columns=columns, header_row=1, data_start_row=max(4, depth + 1))
    return WorkflowConfig(name=profile.name, fields=fields, outputs=[OutputFile(name="标准商品表.xlsx", sheets=[sheet])])


class FieldProfileService:
    def __init__(self, store: WorkflowStore):
        self.store = store

    def _id(self, tenant):
        return "fieldconfig_" + hashlib.sha256(tenant.encode()).hexdigest()[:32]

    def _public(self, obj, tenant):
        result = deepcopy(self.store.public(obj))
        result["is_default"] = obj["id"] == self._id(tenant)
        # The legacy published definition and task snapshots stay byte-for-byte intact.
        result["payload"].setdefault("template_name", "通用商品模板" if result["is_default"] else result["payload"]["draft"]["name"])
        result["payload"].setdefault("published_template_name", "通用商品模板" if result["is_default"] else (result["payload"].get("published") or result["payload"]["draft"])["name"])
        return result

    def get(self, tenant, profile_id=None):
        if profile_id and profile_id != self._id(tenant):
            with self.store.engine.connect() as con:
                return self._public(self.store.get(con, profile_id, tenant, "fieldconfig"), tenant)
        with self.store.engine.begin() as con:
            try:
                obj = self.store.get(con, self._id(tenant), tenant, "fieldconfig")
            except WorkflowError as exc:
                if exc.status != 404:
                    raise
                obj = self.store.create(con, tenant, "fieldconfig", {
                    "draft": initial_profile().model_dump(mode="json"), "published": None, "version": 0,
                    "template_name": "通用商品模板",
                }, object_id=self._id(tenant))
        return self._public(obj, tenant)

    def list(self, tenant):
        self.get(tenant)
        with self.store.engine.connect() as con:
            rows = con.execute(select(objects).where(objects.c.tenant_id == tenant, objects.c.kind == "fieldconfig")
                               .order_by(objects.c.created_at, objects.c.id)).mappings().all()
        items = [self._public(dict(row), tenant) for row in rows]
        items.sort(key=lambda item: not item["is_default"])
        return {"items": items}

    def create(self, tenant, request: NewFieldProfile):
        if request.source_id:
            source = self.get(tenant, request.source_id)
            profile = FieldProfile.model_validate(source["payload"]["draft"])
            profile.name = request.name
        else:
            profile = FieldProfile(name=request.name, fixed_fields=[
                ConfiguredField(id="product_name", title="商品名称", required=True),
            ])
        with self.store.engine.begin() as con:
            obj = self.store.create(con, tenant, "fieldconfig", {
                "draft": profile.model_dump(mode="json"), "published": None, "version": 0,
                "template_name": request.name, "copied_from": request.source_id,
            })
        return self._public(obj, tenant)

    def save(self, tenant, request: ProfileEdit, profile_id=None):
        current = self.get(tenant, profile_id)
        with self.store.engine.begin() as con:
            obj = self.store.claim(con, current["id"], tenant, request.base_revision, "fieldconfig")
            previous = obj["payload"].get("published")
            if previous and {f["id"] for f in previous["fixed_fields"]} - {f.id for f in request.profile.fixed_fields}:
                raise WorkflowError("已发布固定字段不能删除或更改 ID，请停用该字段。")
            obj["payload"]["draft"] = request.profile.model_dump(mode="json")
            obj["payload"]["template_name"] = request.profile.name
            obj["status"] = "draft"
            self.store.save(con, obj)
        return self._public(obj, tenant)

    def publish(self, tenant, revision, profile_id=None):
        with self.store.engine.begin() as con:
            obj = self.store.claim(con, profile_id or self._id(tenant), tenant, revision, "fieldconfig")
            profile = FieldProfile.model_validate(obj["payload"]["draft"])
            obj["payload"].update(published=profile.model_dump(mode="json"), version=obj["payload"]["version"] + 1)
            obj["payload"]["published_template_name"] = obj["payload"].get("template_name", profile.name)
            obj["status"] = "published"
            self.store.save(con, obj)
        return self._public(obj, tenant)

    def snapshot(self, tenant, profile_id=None):
        obj = self.get(tenant, profile_id)
        if not obj["payload"].get("published"):
            raise WorkflowError("请先发布所选模板的字段配置，再上传供应商表。", 409)
        profile = FieldProfile.model_validate(obj["payload"]["published"])
        snapshot = {"profile": profile.model_dump(mode="json"), "version": obj["payload"]["version"],
                    "profile_id": obj["id"],
                    "template_name": obj['payload']['published_template_name'],
                    "compatibility_version": obj['payload'].get('compatibility_version', 0),
                    "matching_scope": "all_fields",
                    "sha256": content_hash(profile.model_dump(mode="json")),
                    "dynamic_source": "platform_import" if profile.platform_source else "local_preview" if profile.local_categories else "not_connected"}
        return compile_profile(profile), snapshot

    def upgrade_specification_types(self, tenant, base_revision):
        """Publish only the two authorized type changes, preserving draft work."""
        from .specification_compatibility import text_specification_profile, COMPATIBILITY_VERSION
        with self.store.engine.begin() as con:
            obj = self.store.claim(con, self._id(tenant), tenant, base_revision, 'fieldconfig')
            for member in ('draft', 'published'):
                if obj['payload'].get(member):
                    profile, changes = text_specification_profile(obj['payload'][member])
                    obj['payload'][member] = profile.model_dump(mode='json')
                    if member == 'published' and changes:
                        obj['payload']['version'] += 1
            obj['payload']['compatibility_version'] = COMPATIBILITY_VERSION
            self.store.save(con, obj)
        return self.store.public(obj)
