"""Import dynamic field definitions through a managed or administrator-entered connection."""
from __future__ import annotations

import os
import hashlib
import json
from uuid import uuid4
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, model_validator

from ..models import StrictModel
from ..standard_sources import ConnectionRegistry, ManagedHttpSource, ManagedConnection, FieldDefinitionConnectionConfig
from .field_profiles import ConfiguredField, FieldProfile, LocalCategory
from .models import WorkflowError


class PlatformFieldRequest(StrictModel):
    connection_id: str = Field(min_length=1, max_length=128)


class DirectPlatformSettings(StrictModel):
    base_url: str = Field(min_length=1, max_length=500)
    path: str = Field(default='/fields', max_length=500)
    method: str = 'GET'
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(''))
    clear_key: bool = False

    @model_validator(mode='after')
    def validate_endpoint(self):
        self.base_url = self.base_url.strip().rstrip('/')
        parsed = urlsplit(self.base_url)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in {'', '/'}):
            raise ValueError('请输入不含路径、参数或密钥的接口根地址，例如 https://api.example.com')
        if parsed.scheme == 'http' and not insecure_http_allowed():
            raise ValueError('平台接口必须使用 HTTPS；HTTP 仅可在开发环境显式开启')
        if self.method not in {'GET', 'POST'}:
            raise ValueError('接口请求方式仅支持 GET 或 POST')
        FieldDefinitionConnectionConfig(path=self.path, method=self.method)
        return self


def insecure_http_allowed() -> bool:
    return (os.environ.get('EXCEL_AUDITOR_ALLOW_INSECURE_PLATFORM_HTTP') == '1'
            and os.environ.get('EXCEL_AUDITOR_ENV', 'development').lower() not in {'prod', 'production'})


def private_network_allowed() -> bool:
    return os.environ.get('EXCEL_AUDITOR_ALLOW_PRIVATE_PLATFORM') == '1'


class DirectPlatformConnection:
    def __init__(self, root: Path, tenant: str):
        self.path = root / 'settings' / ('platform-' + hashlib.sha256(tenant.encode()).hexdigest() + '.json')

    def _read(self) -> dict:
        return json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}

    def public(self) -> dict:
        saved = self._read()
        return {'base_url': saved.get('base_url', ''), 'path': saved.get('path', '/fields'),
                'method': saved.get('method', 'GET'), 'has_key': bool(saved.get('secret')),
                'configured': bool(saved.get('base_url')), 'http_allowed': insecure_http_allowed(),
                'private_network_allowed': private_network_allowed()}

    def save(self, settings: DirectPlatformSettings) -> dict:
        old = self._read()
        key = settings.api_key.get_secret_value().strip()
        if '\r' in key or '\n' in key:
            raise WorkflowError('平台密钥不能包含换行。', 422, 'PLATFORM_KEY_INVALID')
        # Never forward a retained token when its destination changes.
        retain = (old.get('base_url') == settings.base_url and old.get('path') == settings.path
                  and not settings.clear_key)
        secret = protect(key, self.path.parent) if key and not settings.clear_key else old.get('secret', '') if retain else ''
        value = {'base_url': settings.base_url, 'path': settings.path, 'method': settings.method,
                 'secret': secret}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.' + uuid4().hex + '.tmp')
        descriptor = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                json.dump(value, handle, ensure_ascii=False)
            temporary.replace(self.path)
            self.path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)
        return self.public()

    def preview(self, tenant: str, *, transport=None, resolver=None) -> dict:
        saved = self._read()
        if not saved.get('base_url'):
            raise WorkflowError('请先填写并保存平台接口地址。', 409, 'PLATFORM_NOT_CONFIGURED')
        try:
            settings = DirectPlatformSettings(base_url=saved['base_url'], path=saved['path'], method=saved['method'])
            connection = ManagedConnection(
                id='direct-platform', base_url=settings.base_url, allowed_paths=[settings.path],
                allow_private_network=private_network_allowed(), allow_insecure_http=insecure_http_allowed(),
                max_response_bytes=2 * 1024 * 1024,
                field_definition=FieldDefinitionConnectionConfig(title='直接连接的平台', path=settings.path, method=settings.method, tenant_ids=[tenant]))
            registry = type('DirectRegistry', (), {'get': lambda _self, _id: connection})()
            managed = ManagedHttpSource(registry, transport=transport, resolver=resolver)
            secret = unprotect(saved['secret'], self.path.parent) if saved.get('secret') else ''
            return preview(tenant, connection.id, managed, auth_secret_override=secret)
        except WorkflowError:
            raise
        except (ValidationError, ValueError) as exc:
            raise WorkflowError('平台接口配置无效，请检查地址、路径和当前网络策略。', 422, 'PLATFORM_CONFIG_INVALID') from exc


def protect(secret: str, directory: Path) -> str:
    if os.name == 'nt' and os.environ.get('EXCEL_AUDITOR_ENV', '').lower() not in {'prod', 'production'}:
        from .ai import _protect
        return 'dpapi:' + _protect(secret)
    from cryptography.fernet import Fernet
    return 'fernet:' + Fernet(_fernet_key(directory)).encrypt(secret.encode()).decode()


def unprotect(secret: str, directory: Path) -> str:
    if secret.startswith('dpapi:') and os.name == 'nt':
        from .ai import _protect
        return _protect(secret[6:], decrypt=True)
    if secret.startswith('fernet:'):
        from cryptography.fernet import Fernet
        return Fernet(_fernet_key(directory)).decrypt(secret[7:].encode()).decode()
    raise WorkflowError('平台密钥无法解密，请重新填写并保存。', 503, 'PLATFORM_KEY_UNAVAILABLE')


def _fernet_key(directory: Path) -> bytes:
    from cryptography.fernet import Fernet
    mounted = os.environ.get('EXCEL_AUDITOR_PLATFORM_KEY_FILE')
    if mounted:
        try:
            key = Path(mounted).read_bytes().strip()
            Fernet(key)
            return key
        except (OSError, ValueError) as exc:
            raise WorkflowError('平台密钥加密文件无法读取或格式无效。', 503, 'PLATFORM_KEY_UNAVAILABLE') from exc
    if os.environ.get('EXCEL_AUDITOR_ENV', '').lower() in {'prod', 'production'}:
        raise WorkflowError('生产环境需要配置平台密钥加密文件。', 503, 'PLATFORM_KEY_REQUIRED')
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / '.platform-encryption-key'
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(Fernet.generate_key())
    except FileExistsError:
        pass
    return path.read_bytes().strip()


def source() -> ManagedHttpSource | None:
    path = os.environ.get('EXCEL_AUDITOR_CONNECTIONS')
    if not path:
        return None
    try:
        return ManagedHttpSource(ConnectionRegistry(Path(path)))
    except (OSError, ValueError) as exc:
        raise WorkflowError('平台连接配置无法读取，请管理员检查服务器连接文件。', 503, 'PLATFORM_CONNECTION_CONFIG') from exc


def available(tenant: str, managed: ManagedHttpSource | None = None) -> dict:
    managed = managed if managed is not None else source()
    if managed is None:
        return {'items': [], 'configured': False}
    return {'configured': True, 'items': [
        {'id': connection.id, 'title': definition.title}
        for connection in managed.registry.connections.values()
        if (definition := connection.field_definition) and tenant in definition.tenant_ids
    ]}


def preview(tenant: str, connection_id: str, managed: ManagedHttpSource | None = None,
            *, auth_secret_override: str | None = None) -> dict:
    managed = managed if managed is not None else source()
    if managed is None:
        raise WorkflowError('尚未配置平台字段连接，请管理员先设置服务器连接。', 422, 'PLATFORM_NOT_CONFIGURED')
    try:
        connection = managed.registry.get(connection_id)
    except KeyError as exc:
        raise WorkflowError('平台字段连接不存在或无权使用。', 404, 'PLATFORM_CONNECTION_NOT_FOUND') from exc
    definition = connection.field_definition
    if definition is None or tenant not in definition.tenant_ids:
        raise WorkflowError('平台字段连接不存在或无权使用。', 404, 'PLATFORM_CONNECTION_NOT_FOUND')
    try:
        payload, _metadata = managed.fetch_object(connection_id, definition.path, definition.method,
                                                   static_parameters=definition.static_parameters,
                                                   auth_secret_override=auth_secret_override)
    except (ValueError, OSError) as exc:
        raise WorkflowError('读取平台字段失败，请检查连接地址、密钥、路径及平台服务状态。', 502, 'PLATFORM_FETCH_FAILED') from exc
    body = payload.get('data', payload)
    if not isinstance(body, dict) or not isinstance(body.get('categories'), list):
        raise WorkflowError('平台字段响应应包含 categories 数组。', 422, 'PLATFORM_SCHEMA_INVALID')
    try:
        categories = [LocalCategory.model_validate(category) for category in body['categories']]
        # Apply the same uniqueness, count, title and field-type rules as a template.
        FieldProfile(fixed_fields=[ConfiguredField(id='product_name', title='商品名称')],
                     local_categories=categories)
    except (ValidationError, ValueError) as exc:
        raise WorkflowError('平台字段格式无效：请检查类目、字段 ID、名称、类型和重复项。', 422, 'PLATFORM_SCHEMA_INVALID') from exc
    if not categories:
        raise WorkflowError('平台没有返回可导入的动态字段类目。', 422, 'PLATFORM_SCHEMA_EMPTY')
    return {
        'connection_id': connection_id,
        'title': definition.title,
        'schema_version': str(body.get('schema_version', ''))[:100],
        'fetched_at': datetime.now(timezone.utc).isoformat(),
        'categories': [category.model_dump(mode='json') for category in categories],
        'category_count': len(categories),
        'field_count': sum(len(c.attributes) + len(c.specifications) for c in categories),
    }
