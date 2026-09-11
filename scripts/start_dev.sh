#!/usr/bin/env bash
# 孙武侦查官 本机开发联调启动脚本（WSL Ubuntu / venv=/root/.venvs/inves）
#
# 用法：
#   bash scripts/start_dev.sh              # 同时启动 API(8000) + Worker(2 并发)，日志落 logs/
#   bash scripts/start_dev.sh api          # 只启动 API
#   bash scripts/start_dev.sh worker       # 只启动 Worker
#   bash scripts/start_dev.sh stop         # 停止 API + Worker
#   bash scripts/start_dev.sh stop api     # 只停止 API（stop worker 同理）
#   bash scripts/start_dev.sh restart      # 重启 API + Worker
#   bash scripts/start_dev.sh restart api  # 只重启 API（restart worker 同理）
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
    # 仅在本脚本启动过服务时才有清理动作（stop 模式 PID 为空，静默退出）
    if [ -n "$WORKER_PID" ] || [ -n "$API_PID" ]; then
        echo ""
        echo "[start_dev] 停止服务..."
        [ -n "$WORKER_PID" ] && kill "$WORKER_PID" 2>/dev/null || true
        [ -n "$API_PID" ] && kill "$API_PID" 2>/dev/null || true
        wait 2>/dev/null || true
        echo "[start_dev] 已停止"
    fi
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

usage_exit() {
    echo "用法: bash scripts/start_dev.sh [api|worker|all(默认)]" >&2
    echo "      bash scripts/start_dev.sh stop    [api|worker|all(默认)]   # 停止服务" >&2
    echo "      bash scripts/start_dev.sh restart [api|worker|all(默认)]   # 重启服务" >&2
    exit 2
}

# 按命令行特征查找并停止进程：先 SIGTERM 优雅退出，5s 后仍存活则 SIGKILL
stop_by_pattern() {
    local name="$1" pattern="$2" pids
    pids=$(pgrep -f "$pattern" || true)
    if [ -z "$pids" ]; then
        echo "[start_dev] $name 未在运行"
        return 0
    fi
    echo "[start_dev] 停止 $name (PID: $(echo "$pids" | tr '\n' ' '))"
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    for _ in $(seq 1 10); do
        pids=$(pgrep -f "$pattern" || true)
        [ -z "$pids" ] && break
        sleep 0.5
    done
    pids=$(pgrep -f "$pattern" || true)
    if [ -n "$pids" ]; then
        echo "[start_dev] $name 5s 内未退出，SIGKILL 强制结束"
        # shellcheck disable=SC2086
        kill -9 $pids 2>/dev/null || true
    fi
    echo "[start_dev] $name 已停止"
}

stop_api()    { stop_by_pattern "API"    "uvicorn server\.app\.main:app"; }
stop_worker() { stop_by_pattern "Worker" "server\.run_worker"; }

stop_target() {
    case "$1" in
        api)    stop_api ;;
        worker) stop_worker ;;
        all)    stop_worker; stop_api ;;
    esac
}

ACTION="${1:-all}"
TARGET="all"

case "$ACTION" in
    api|worker|all)
        ;;
    stop|restart)
        TARGET="${2:-all}"
        case "$TARGET" in
            api|worker|all) ;;
            *) usage_exit ;;
        esac
        stop_target "$TARGET"
        if [ "$ACTION" = "stop" ]; then
            exit 0
        fi
        ACTION="$TARGET"   # restart：停止后按目标重新启动
        ;;
    *)
        usage_exit
        ;;
esac

case "$ACTION" in
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
esac
