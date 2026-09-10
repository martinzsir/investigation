#!/usr/bin/env bash
# Spike 用：在独立 venv 安装 AgentScope（不动 /root/.venvs/inves）
set -euo pipefail
# 清华 403 时回落阿里云（AGENTS.md 已知坑）
MIRROR=https://mirrors.aliyun.com/pypi/simple

python3 -m venv /root/.venvs/agentscope
/root/.venvs/agentscope/bin/pip install -q -i "$MIRROR" agentscope \
  || /root/.venvs/agentscope/bin/pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple agentscope
/root/.venvs/agentscope/bin/python - <<'PY'
import agentscope, sys
print("python", sys.version.split()[0])
print("agentscope", agentscope.__version__)
PY
