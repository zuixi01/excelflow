"""Create a private, editable production config without committing secrets."""

from __future__ import annotations

import argparse
import base64
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write_private(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def initialize(directory: Path) -> Path:
    if os.name != "nt" and os.getgid() == 0:
        raise ValueError("请用专用部署账号初始化，避免给容器加入 root 组")
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        directory.chmod(0o700)
    env_file = directory / "production.env"
    platform_key = directory / "platform-key"
    if env_file.exists() or platform_key.exists():
        raise FileExistsError("生产配置或平台密钥已存在，不会覆盖")
    template = (ROOT / "deploy" / "env.example").read_text(encoding="utf-8")
    replacements = {
        "EXCEL_AUDITOR_API_TOKEN=replace-with-at-least-32-random-characters":
            "EXCEL_AUDITOR_API_TOKEN=" + secrets.token_hex(32),
        "EXCEL_AUDITOR_ADMIN_PASSWORD=replace-with-secret":
            "EXCEL_AUDITOR_ADMIN_PASSWORD=" + secrets.token_urlsafe(32),
        "MODEL_KEY_FILE=/secure/excelflow/model-key":
            "MODEL_KEY_FILE=" + str(directory / "model-key"),
        "PLATFORM_KEY_FILE=/secure/excelflow/platform-key":
            "PLATFORM_KEY_FILE=" + str(platform_key),
        "SECRET_READ_GID=1000":
            "SECRET_READ_GID=" + str(getattr(os, "getgid", lambda: 0)()),
        "TLS_CERT_FILE=/secure/excelflow/tls/fullchain.pem":
            "TLS_CERT_FILE=" + str(directory / "fullchain.pem"),
        "TLS_KEY_FILE=/secure/excelflow/tls/privkey.pem":
            "TLS_KEY_FILE=" + str(directory / "privkey.pem"),
    }
    for before, after in replacements.items():
        if before not in template:
            raise ValueError(f"配置模板缺少 {before.split('=')[0]}")
        template = template.replace(before, after, 1)
    write_private(platform_key, base64.urlsafe_b64encode(secrets.token_bytes(32)).decode() + "\n")
    if os.name != "nt":
        platform_key.chmod(0o640)
    write_private(env_file, template)
    return env_file


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    path = initialize(args.directory)
    print(f"已创建 {path}，请填写数据库、Redis、S3、模型密钥、域名和证书后运行部署预检。")


if __name__ == "__main__":
    main()
