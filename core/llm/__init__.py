"""
core.llm —— LLM 治理基座（REQ-038/039）。

本包不含任何真实模型调用：
  - redact.py：策略装载（fail-closed）、PII 脱敏、脱敏上下文、调用闸门与审计日志；
  - guard.py：提示注入防护（不可信内容分框、注入扫描、候选白名单过滤）。

所有"模型调用"必须经 core.llm.redact.call_llm 闸门，且仅能通过注入的 fake_invoke
（测试/未来受控接入）执行；生产环境 network=isolated + allowed_models=[] 双保险全拒。
"""
