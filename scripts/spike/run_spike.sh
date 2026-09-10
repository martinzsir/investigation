#!/usr/bin/env bash
# Spike 运行器：加载密钥文件后跑 AgentScope×MCP 互通脚本。
# 优先级：同目录 .env（已在 .gitignore，不入库）> /root/.spike_llm_env
# .env 支持两种行：export KEY=value 或 KEY=value（# 开头注释跳过）
set -euo pipefail
SPIKE_DIR=/mnt/d/dev/inves_duckdb/scripts/spike
load_env() {
  local f="$1"
  [ -f "$f" ] || return 0
  set -a
  # shellcheck disable=SC1090
  source "$f"
  set +a
}
if [ -f "$SPIKE_DIR/.env" ]; then
  load_env "$SPIKE_DIR/.env"
elif [ -f /root/.spike_llm_env ]; then
  load_env /root/.spike_llm_env
else
  echo "[run_spike] 未找到 .env：Phase B 将跳过（只跑 A/C）" >&2
fi
exec /root/.venvs/agentscope/bin/python "$SPIKE_DIR/agentscope_mcp_spike.py"
