#!/usr/bin/env bash
# 零依赖启动：python3 -m benchguard
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m benchguard
