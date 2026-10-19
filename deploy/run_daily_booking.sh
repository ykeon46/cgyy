#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"
if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi
# 可选的本机浏览器、共享库和显示服务设置，不进入 Git。
if [[ -f artifacts/runtime/environment.sh ]]; then
  source artifacts/runtime/environment.sh
fi

config_path="$project_root/configs/default.yaml"
if [[ -f artifacts/web/config.yaml ]]; then config_path="$project_root/artifacts/web/config.yaml"; fi
booking_command=("$project_root/.venv/bin/python" "$project_root/scheduled_booking.py"
  --config "$config_path" --daily --headed --payment-qr --captcha-repo "$project_root/artifacts/Text_select_captcha" "$@")
if [[ -n "${DISPLAY:-}" ]]; then
  exec "${booking_command[@]}"
elif command -v xvfb-run >/dev/null 2>&1; then
  exec xvfb-run -a -s '-screen 0 1440x1000x24 -nolisten tcp' "${booking_command[@]}"
else
  echo '每日快速预约需要 DISPLAY 或已安装的 xvfb-run / Xvfb / xauth。' >&2
  exit 1
fi
