#!/usr/bin/env bash
# 孙武侦查官 本机开发联调启动脚本（WSL Ubuntu / venv=/root/.venvs/inves）
#
# 用法：
#   bash scripts/start_dev.sh              # 同时启动 API(8000) + Worker(2 并发)，日志落 logs/
#   bash scripts/start_dev.sh api          # 只启动 API
#   bash scripts/start_dev.sh worker       # 只启动 Worker
#
# 环境变量（均有默认值，可覆盖）：
#   SUNZI_PY       Python 解释器（默认 /root/.venvs/inves/bin/python）
#   SUNZI_PORT     API 端口（默认 8000）
#   SUNZI_WORKERS  Worker 并发数（默认 2）
#   SUNZI_META_DB  元数据 SQLite（默认 meta/meta.db）
#   SUNZI_CASES_ROOT 案件根目录（默认 cases）
set -euo pipefail

cd "$(dirname "$0")/.."

PY="${SUNZI_PY:-/root/.venvs/inves/bin/python}"
PORT="${SUNZI_PORT:-8000}"
WORKERS="${SUNZI_WORKERS:-2}"
LOG_DIR="logs"
mkdir -p "$LOG_DIR"

API_LOG="$LOG_DIR/api.log"
WORKER_LOG="$LOG_DIR/worker.log"

API_PID=""
WORKER_PID=""

cleanup() {
    echo ""
    echo "[start_dev] 停止服务..."
    [ -n "$WORKER_PID" ] && kill "$WORKER_PID" 2>/dev/null || true
    [ -n "$API_PID" ] && kill "$API_PID" 2>/dev/null || true
    wait 2>/dev/null || true
    echo "[start_dev] 已停止"
}
trap cleanup EXIT INT TERM

start_api() {
    echo "[start_dev] 启动 API: uvicorn 0.0.0.0:$PORT (--reload)"
    # shellcheck disable=SC2016
    "$PY" -m uvicorn server.app.main:app --reload \
        --host 0.0.0.0 --port "$PORT" >"$API_LOG" 2>&1 &
    API_PID=$!
    echo "[start_dev] API PID=$API_PID 日志: $API_LOG"

    echo -n "[start_dev] 等待 API 就绪"
    for _ in $(seq 1 30); do
        if curl -sf "http://localhost:$PORT/api/v1/health" >/dev/null 2>&1; then
            echo ""
            echo "[start_dev] API 健康探针 OK: http://localhost:$PORT/api/v1/health"
            return 0
        fi
        echo -n "."
        sleep 1
    done
    echo ""
    echo "[start_dev] API 30s 内未就绪，请看日志: $API_LOG" >&2
    tail -20 "$API_LOG" >&2 || true
    exit 1
}

start_worker() {
    echo "[start_dev] 启动 Worker: workers=$WORKERS（-u 无缓冲）"
    "$PY" -u -m server.run_worker --workers "$WORKERS" >"$WORKER_LOG" 2>&1 &
    WORKER_PID=$!
    echo "[start_dev] Worker PID=$WORKER_PID 日志: $WORKER_LOG"
    sleep 2
    if ! kill -0 "$WORKER_PID" 2>/dev/null; then
        echo "[start_dev] Worker 启动失败，日志: $WORKER_LOG" >&2
        tail -20 "$WORKER_LOG" >&2 || true
        exit 1
    fi
    echo "[start_dev] Worker 运行中"
}

case "${1:-all}" in
    api)
        start_api
        echo "[start_dev] API 前台挂起中（Ctrl+C 停止）"
        wait "$API_PID"
        ;;
    worker)
        start_worker
        echo "[start_dev] Worker 前台挂起中（Ctrl+C 停止）"
        wait "$WORKER_PID"
        ;;
    all)
        start_api
        start_worker
        echo ""
        echo "[start_dev] 全部就绪："
        echo "  API    : http://localhost:$PORT  (PID $API_PID,  日志 $API_LOG)"
        echo "  Worker : workers=$WORKERS        (PID $WORKER_PID, 日志 $WORKER_LOG)"
        echo "  登录账户: demo / demo12345"
        echo "  Ctrl+C 停止全部服务"
        wait
        ;;
    *)
        echo "用法: bash scripts/start_dev.sh [api|worker|all(默认)]" >&2
        exit 2
        ;;
esac
