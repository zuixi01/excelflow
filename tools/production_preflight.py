"""Fail before a production rollout when required deployment inputs are unsafe."""

from __future__ import annotations

import argparse
import base64
import binascii
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from excel_auditor.security_config import validate_production_environment  # noqa: E402

REQUIRED = (
    "DATABASE_URL", "REDIS_URL", "S3_BUCKET", "EXCEL_AUDITOR_API_TOKEN",
    "EXCEL_AUDITOR_ADMIN_PASSWORD", "MODEL_KEY_FILE", "PLATFORM_KEY_FILE",
    "PUBLIC_HOSTNAME", "TLS_CERT_FILE", "TLS_KEY_FILE",
    "EXCEL_AUDITOR_TRUSTED_PROXY_IPS", "IMAGE_OWNER",
    "SECRET_READ_GID",
    "SERVICE_CA_FILE",
)
PLACEHOLDERS = ("replace-with", "example.com", "user:password", "git-sha-required")


def read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key):
            raise ValueError(f"配置文件第 {number} 行不是 KEY=VALUE")
        if key in result:
            raise ValueError(f"配置文件重复定义 {key}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
    return result


def _private_file(value: str, label: str, *, read_gid: int | None = None) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f"{label} 必须是已存在的绝对文件路径")
    if not path.read_bytes().strip():
        raise ValueError(f"{label} 不能为空")
    if os.name != "nt":
        details = path.stat()
        mode = stat.S_IMODE(details.st_mode)
        if read_gid is None:
            if mode & 0o077:
                raise ValueError(f"{label} 只能由文件所有者读取（chmod 600）")
        elif details.st_gid != read_gid or mode & 0o037 or not mode & 0o040:
            raise ValueError(f"{label} 必须属于 SECRET_READ_GID 且使用组只读权限（chmod 640）")
    return path


def _openssl(*args: str) -> bytes:
    try:
        result = subprocess.run(["openssl", *args], capture_output=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("无法运行 openssl 检查 HTTPS 证书") from exc
    if result.returncode:
        raise ValueError("HTTPS 证书、私钥或域名校验失败")
    return result.stdout


def validate(values: dict[str, str], *, env_path: Path) -> None:
    missing = [key for key in REQUIRED if not values.get(key)]
    if missing:
        raise ValueError("缺少必填配置：" + ", ".join(missing))
    if any(marker in value.lower() for value in values.values() for marker in PLACEHOLDERS):
        raise ValueError("仍有示例占位值，请填写真实地址和凭据")
    if values.get("S3_ENDPOINT_URL") and (not values.get("S3_ACCESS_KEY_ID") or not values.get("S3_SECRET_ACCESS_KEY")):
        raise ValueError("自定义 S3/MinIO 接口需要 S3_ACCESS_KEY_ID 和 S3_SECRET_ACCESS_KEY")
    if os.name != "nt" and stat.S_IMODE(env_path.stat().st_mode) & 0o077:
        raise ValueError("生产配置文件含密钥，建议 chmod 600")
    if len(values["EXCEL_AUDITOR_ADMIN_PASSWORD"]) < 16:
        raise ValueError("管理员初始密码至少需要 16 个字符")
    if values["EXCEL_AUDITOR_TRUSTED_PROXY_IPS"] == "127.0.0.1":
        raise ValueError("Docker 中的 Web 代理并非 127.0.0.1，请设置私有代理来源")
    if not values["SECRET_READ_GID"].isdigit():
        raise ValueError("SECRET_READ_GID 必须是主机上的数字组 ID")
    read_gid = int(values["SECRET_READ_GID"])
    if os.name != "nt" and read_gid == 0:
        raise ValueError("SECRET_READ_GID 不能是 root 组")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", values["IMAGE_OWNER"]):
        raise ValueError("IMAGE_OWNER 应为发布镜像的 GitHub 所有者")
    hostname = values["PUBLIC_HOSTNAME"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", hostname):
        raise ValueError("PUBLIC_HOSTNAME 只能填写主机名，不含协议、端口和路径")
    port = values.get("WEB_PORT", "8080")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("WEB_PORT 必须是有效端口")
    if ca_file := values.get("TLS_CA_FILE"):
        if not Path(ca_file).is_absolute() or not Path(ca_file).is_file():
            raise ValueError("TLS_CA_FILE 必须是已存在的绝对文件路径")
    service_ca = Path(values["SERVICE_CA_FILE"])
    if not service_ca.is_absolute() or not service_ca.is_file() or not service_ca.read_bytes().strip():
        raise ValueError("SERVICE_CA_FILE 必须是已存在的 CA 证书包")
    if os.name != "nt":
        details = service_ca.stat()
        mode = stat.S_IMODE(details.st_mode)
        if not mode & 0o004 and not (details.st_gid == read_gid and mode & 0o040):
            raise ValueError("SERVICE_CA_FILE 对非 root 容器用户不可读")
    _openssl("x509", "-in", str(service_ca), "-noout")

    runtime = dict(values)
    runtime.update(
        EXCEL_AUDITOR_ENV="production",
        EXCEL_AUDITOR_REQUIRE_AUTH="1",
        EXCEL_AUDITOR_MODEL_KEY_FILE="/run/secrets/model-key",
        EXCEL_RENDERER_COMMAND="/app/renderer/ExcelRenderer",
    )
    validate_production_environment(runtime)
    for key in ("DATABASE_URL", "REDIS_URL", "S3_ENDPOINT_URL"):
        if values.get(key) and not urlsplit(values[key]).hostname:
            raise ValueError(f"{key} 缺少有效主机名")

    _private_file(values["MODEL_KEY_FILE"], "MODEL_KEY_FILE", read_gid=read_gid)
    platform = _private_file(values["PLATFORM_KEY_FILE"], "PLATFORM_KEY_FILE", read_gid=read_gid)
    try:
        decoded = base64.urlsafe_b64decode(platform.read_bytes().strip())
    except (ValueError, binascii.Error) as exc:
        raise ValueError("PLATFORM_KEY_FILE 不是有效 Fernet 密钥") from exc
    if len(decoded) != 32:
        raise ValueError("PLATFORM_KEY_FILE 不是有效 Fernet 密钥")

    cert = Path(values["TLS_CERT_FILE"])
    if not cert.is_absolute() or not cert.is_file():
        raise ValueError("TLS_CERT_FILE 必须是已存在的绝对文件路径")
    private_key = _private_file(values["TLS_KEY_FILE"], "TLS_KEY_FILE")
    _openssl("x509", "-in", str(cert), "-noout", "-checkend", "2592000")
    host_option = "-checkip" if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", hostname) else "-checkhost"
    host_result = _openssl("x509", "-in", str(cert), "-noout", host_option, hostname).decode(errors="replace")
    # OpenSSL x509 -checkhost can exit 0 even when its text says NOT matched.
    if " does match certificate" not in host_result:
        raise ValueError("HTTPS 证书不包含 PUBLIC_HOSTNAME")
    certificate_public = _openssl("x509", "-in", str(cert), "-pubkey", "-noout")
    key_public = _openssl("pkey", "-in", str(private_key), "-pubout")
    if certificate_public != key_public:
        raise ValueError("HTTPS 证书与私钥不匹配")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        if not args.env_file.is_file():
            raise ValueError("生产配置文件不存在")
        validate(read_env(args.env_file), env_path=args.env_file)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"生产部署预检失败：{exc}", file=sys.stderr)
        return 1
    print("生产部署预检通过：配置、密钥文件和 HTTPS 证书有效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
