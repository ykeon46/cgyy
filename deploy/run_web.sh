#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"
if [[ -f artifacts/runtime/environment.sh ]]; then source artifacts/runtime/environment.sh; fi
exec "$project_root/.venv/bin/python" "$project_root/web_app.py" "$@"
