from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config

from .security_config import validate_production_environment


def run() -> None:
    validate_production_environment()
    config_path = Path(os.environ.get("EXCEL_AUDITOR_ALEMBIC_CONFIG", Path.cwd() / "alembic.ini"))
    if os.environ.get("DATABASE_URL"):
        config = Config(str(config_path))
        command.upgrade(config, "head")
    workers = int(os.environ.get("EXCEL_AUDITOR_API_WORKERS", "1"))
    if not 1 <= workers <= 16:
        raise ValueError("EXCEL_AUDITOR_API_WORKERS must be between 1 and 16")
    options = {"workers": workers} if workers > 1 else {}
    if trusted_proxy := os.environ.get("EXCEL_AUDITOR_TRUSTED_PROXY_IPS"):
        options["forwarded_allow_ips"] = trusted_proxy
    uvicorn.run("excel_auditor.api:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), **options)
