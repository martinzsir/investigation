"""server/ —— Web API 服务端（M1）。

与 core/ 的边界（见 .trae/documents/web/M1_plan.md）：
  - core/ 是确定性侦查内核，本包是其 HTTP/Worker 薄编排层；
  - 开库一律经 server.app.store.StoreFactory（grep 门禁：server/ 内不得
    出现 core.store.Store 的无参实例化；server/app/ 除 store/ 外不得
    直接 duckdb 连接）；
  - 只读查询走 core.gateway.OntologyReadGateway，计算走 FunctionExecutor，
    写走 ActionExecutor——本层不写业务 SQL。
"""
