"""Smoke-test the released web image as both the site and its TLS edge."""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOCK_API = """
import http.server
import json

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        result = json.dumps({
            'xff': self.headers.get('X-Forwarded-For'),
            'xfp': self.headers.get('X-Forwarded-Proto'),
            'host': self.headers.get('Host'),
        }).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(result)

    def log_message(self, *args):
        pass

http.server.HTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
"""


def command(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, capture_output=True, text=True, timeout=90)
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr[-600:]}")
    return result


def verify(web_image: str, api_image: str) -> dict[str, str | None]:
    name = "excelflow_proxy_" + uuid.uuid4().hex[:10]
    containers: list[str] = []
    curl = shutil.which("curl") or shutil.which("curl.exe")
    if curl is None:
        raise RuntimeError("curl is required for the proxy smoke test")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        config = temporary / "openssl.cnf"
        config.write_text(
            "[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n"
            "[dn]\nCN=excel.internal\n[v3]\nsubjectAltName=DNS:excel.internal\n",
            encoding="utf-8",
        )
        cert, key = temporary / "cert.pem", temporary / "key.pem"
        command("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(key), "-out", str(cert), "-days", "45", "-config", str(config))
        command("docker", "network", "create", name)
        try:
            steps = (
                ("api", ["docker", "run", "-d", "--network", name, "--network-alias", "api",
                         "--name", name + "_api", "--entrypoint", "python", api_image, "-u", "-c", MOCK_API]),
                ("web", ["docker", "run", "-d", "--network", name, "--network-alias", "web",
                         "--name", name + "_web", web_image]),
                ("edge", ["docker", "run", "-d", "--network", name, "--name", name + "_edge",
                          "--user", "0:0", "-p", f"127.0.0.1:{port}:443",
                          "-e", "PUBLIC_HOSTNAME=excel.internal", "-e", "NGINX_ENVSUBST_FILTER=^PUBLIC_HOSTNAME$",
                          "-v", f"{ROOT / 'deploy/nginx-edge.conf'}:/etc/nginx/templates/default.conf.template:ro",
                          "-v", f"{cert}:/run/secrets/tls.crt:ro",
                          "-v", f"{key}:/run/secrets/tls.key:ro", web_image]),
            )
            for role, args in steps:
                command(*args)
                containers.append(name + "_" + role)
            result = None
            for _ in range(30):
                response = command(curl, "--noproxy", "*", "--silent", "--show-error",
                                   "--cacert", str(cert), "--resolve", f"excel.internal:{port}:127.0.0.1",
                                   "-H", "X-Forwarded-For: spoofed-client",
                                   f"https://excel.internal:{port}/api/echo", check=False)
                if response.returncode == 0:
                    result = json.loads(response.stdout)
                    break
                time.sleep(1)
            if result is None:
                raise RuntimeError("TLS proxy did not become ready")
            if result.get("xfp") != "https" or result.get("host") != "excel.internal":
                raise RuntimeError("TLS protocol or Host header was lost between proxies")
            if "spoofed-client" in (result.get("xff") or ""):
                raise RuntimeError("public TLS edge forwarded an untrusted client IP header")
            return result
        finally:
            for container in reversed(containers):
                command("docker", "rm", "-f", container, check=False)
            command("docker", "network", "rm", name, check=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-image", required=True)
    parser.add_argument("--api-image", default="python:3.12-slim-bookworm")
    args = parser.parse_args()
    print("TLS proxy smoke passed:", json.dumps(verify(args.web_image, args.api_image), ensure_ascii=False))


if __name__ == "__main__":
    main()
