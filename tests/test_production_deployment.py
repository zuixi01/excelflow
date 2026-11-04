from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from production_init import initialize
from production_preflight import read_env, validate


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is required by the deployment host")
def test_initialized_config_passes_preflight_only_with_real_services_and_matching_tls(tmp_path):
    directory = tmp_path / "private"
    env_file = initialize(directory)
    with pytest.raises(FileExistsError):
        initialize(directory)
    (directory / "model-key").write_text("test-model-secret\n", encoding="utf-8")
    (directory / "model-key").chmod(0o640)
    config = directory / "openssl.cnf"
    config.write_text(
        "[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n"
        "[dn]\nCN=excel.internal\n[v3]\nsubjectAltName=DNS:excel.internal\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(directory / "privkey.pem"), "-out", str(directory / "fullchain.pem"),
         "-days", "40", "-config", str(config)],
        capture_output=True, check=True,
    )
    (directory / "privkey.pem").chmod(0o600)
    (directory / "service-ca.pem").write_bytes((directory / "fullchain.pem").read_bytes())
    values = read_env(env_file)
    values.update(
        DATABASE_URL="postgresql+psycopg://alice:secret@db.internal:5432/excel?sslmode=require",
        REDIS_URL="rediss://redis.internal:6379/0",
        S3_ENDPOINT_URL="https://s3.internal",
        S3_ACCESS_KEY_ID="test-access-id",
        S3_SECRET_ACCESS_KEY="test-secret-key",
        PUBLIC_HOSTNAME="excel.internal",
        SERVICE_CA_FILE=str(directory / "service-ca.pem"),
    )
    validate(values, env_path=env_file)
    with pytest.raises(ValueError, match="PUBLIC_HOSTNAME"):
        validate({**values, "PUBLIC_HOSTNAME": "other.internal"}, env_path=env_file)
    with pytest.raises(ValueError, match="127.0.0.1"):
        validate({**values, "EXCEL_AUDITOR_TRUSTED_PROXY_IPS": "127.0.0.1"}, env_path=env_file)
    with pytest.raises(ValueError, match="S3_ACCESS_KEY_ID"):
        validate({**values, "S3_ACCESS_KEY_ID": ""}, env_path=env_file)
