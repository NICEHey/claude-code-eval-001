#!/usr/bin/env bash
# 一键启动：不依赖外部数据库（SQLite 文件自动创建在 ./data 下）。
# 用法：./run.sh [host] [port]   例如 ./run.sh 0.0.0.0 8000
set -euo pipefail

cd "$(dirname "$0")"

HOST="${1:-${HOST:-0.0.0.0}}"
PORT="${2:-${PORT:-8000}}"
DB_PATH="${DB_PATH:-data/hydro.db}"

mkdir -p "$(dirname "$DB_PATH")"

echo "启动水文监测服务：http://${HOST}:${PORT} （SQLite: ${DB_PATH}）"
exec python3 -m app.server --host "$HOST" --port "$PORT" --db "$DB_PATH"
