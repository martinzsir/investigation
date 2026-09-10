#!/usr/bin/env bash
# Spike 用：检查外部 LLM 密钥是否在环境中（只报是否存在，不打印值）
for v in DASHSCOPE_API_KEY OPENAI_API_KEY GLM_API_KEY; do
  val="${!v:-}"
  if [ -n "$val" ]; then
    echo "$v=SET(len=${#val})"
  else
    echo "$v=unset"
  fi
done
