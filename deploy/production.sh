#!/usr/bin/env bash
# Run from a clean, tagged Linux checkout. Release CI builds and scans the two
# application images; this script only pulls those exact commit images.
set -Eeuo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
action="${1:-up}"
env_file="${2:-$root/deploy/production.env}"
if [[ ! -f "$env_file" ]]; then
  echo "Missing production config: $env_file (start with deploy/env.example)" >&2
  exit 1
fi
env_file="$(realpath "$env_file")"
cd "$root"

command -v docker >/dev/null || { echo 'Docker is required.' >&2; exit 1; }
docker compose version >/dev/null
command -v python3 >/dev/null || { echo 'Python 3 is required for deployment checks.' >&2; exit 1; }
command -v openssl >/dev/null || { echo 'OpenSSL is required for certificate checks.' >&2; exit 1; }
command -v curl >/dev/null || { echo 'curl is required for readiness checks.' >&2; exit 1; }

python3 tools/production_preflight.py --env-file "$env_file"
commit="$(git rev-parse HEAD)"
image_owner="$(python3 -c 'import sys; sys.path.insert(0,"tools"); from production_preflight import read_env; from pathlib import Path; print(read_env(Path(sys.argv[1]))["IMAGE_OWNER"])' "$env_file")"
export API_IMAGE="ghcr.io/$image_owner/excel-auditor-api:$commit"
export WEB_IMAGE="ghcr.io/$image_owner/excel-auditor-web:$commit"
export IMAGE_PULL_POLICY=always
unset COMPOSE_PROFILES || true
compose=(docker compose --env-file "$env_file" -f deploy/docker-compose.prod.yaml -f deploy/docker-compose.tls.yaml)
"${compose[@]}" config --quiet

case "$action" in
  check)
    if [[ -n "$(git status --porcelain=v1)" ]]; then
      echo 'Checkout has uncommitted files; commit and pull a fixed revision before production deployment.' >&2
      exit 1
    fi
    release_tag="$(git describe --tags --exact-match HEAD 2>/dev/null || true)"
    if [[ ! "$release_tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z]+([.-][0-9A-Za-z]+)*)?$ ]]; then
      echo 'HEAD must have an exact v-prefixed release tag that passed release CI.' >&2
      exit 1
    fi
    echo "Ready to deploy $release_tag ($commit)."
    ;;
  up)
    "$0" check "$env_file"
    "${compose[@]}" pull
    docker run --rm --entrypoint python "$API_IMAGE" -c 'import excel_auditor; from pathlib import Path; assert Path("/app/renderer/ExcelRenderer").is_file()'
    "${compose[@]}" up -d --wait --wait-timeout 360
    web_port="$(python3 -c 'import sys; sys.path.insert(0,"tools"); from production_preflight import read_env; from pathlib import Path; print(read_env(Path(sys.argv[1])).get("WEB_PORT", "8080"))' "$env_file")"
    hostname="$(python3 -c 'import sys; sys.path.insert(0,"tools"); from production_preflight import read_env; from pathlib import Path; print(read_env(Path(sys.argv[1]))["PUBLIC_HOSTNAME"])' "$env_file")"
    curl --fail --silent --show-error "http://127.0.0.1:$web_port/health/ready" >/dev/null
    ca_file="$(python3 -c 'import sys; sys.path.insert(0,"tools"); from production_preflight import read_env; from pathlib import Path; print(read_env(Path(sys.argv[1])).get("TLS_CA_FILE", ""))' "$env_file")"
    curl_args=()
    if [[ -n "$ca_file" ]]; then curl_args=(--cacert "$ca_file"); fi
    curl --fail --silent --show-error "${curl_args[@]}" --resolve "$hostname:443:127.0.0.1" "https://$hostname/health/ready" >/dev/null
    "${compose[@]}" ps
    echo "Deployment is ready at https://$hostname/ (commit $commit)."
    ;;
  status)
    "${compose[@]}" ps
    ;;
  logs)
    "${compose[@]}" logs --tail=100
    ;;
  *)
    echo "Usage: $0 [check|up|status|logs] [absolute-env-file]" >&2
    exit 2
    ;;
esac
