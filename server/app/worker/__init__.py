"""server.app.worker —— 后台任务执行（W-006）。

API 进程只入队；本包含 Worker 循环、任务处理器注册表与 BUILD 编排
（版本化文件构建 + 原子切换，W-007）。
"""
