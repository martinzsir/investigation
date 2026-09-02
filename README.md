# 孙武侦查官 · DuckDB 单机版使用说明

## 为什么用 DuckDB 替代 StarRocks
StarRocks 是"分布式实时 OLAP 的奢侈品"，需要 FE/BE 集群、运维较重。
在**单机 / 定制笔记本 / 单案侦查**场景下，其 MPP + 高并发能力用不上，
因此用 **DuckDB 单文件**承担 L2 温层：零运维、嵌入式、与技能主进程同进程。

| 维度 | StarRocks（原） | DuckDB（现） |
|---|---|---|
| 部署 | FE+BE 集群 | 单个文件 |
| 温层预聚合 | 物化视图 | `CREATE TABLE agg_*` |
| 冷层查询 | Iceberg 外表 | `read_parquet(...)` |
| 增量 | Flink | `scripts/incremental.py` |
| 并发 | 100+ QPS | 单写多读（够单人操作台） |

侦查逻辑（六段 / 五子技能 / 五间交叉）完全不变，仅 SQL 执行器切换。

## 目录结构
```
孙武侦查官_DuckDB版/
├── SKILL.md                  # 技能总卡
├── README.md                 # 本文件
├── run_demo.py               # 端到端演练入口
├── core/
│   ├── __init__.py
│   ├── store.py              # L1 Redis + L2 DuckDB + L3 Parquet 统一接口
│   ├── hypotheses.py         # 假设生成引擎（模式库 / 枚举 / 覆盖度）
│   └── validate.py           # Schema + 红线校验
├── skills/
│   ├── __init__.py
│   ├── miaosuan.py           # 庙算沙盘
│   ├── zhi_ji_zhi_bi.py      # 双向盘点
│   ├── xu_shi.py             # 虚实扫描（DuckDB 扫描 Parquet）
│   ├── qi_zheng.py           # 奇正分工
│   └── yong_jian.py          # 五间交叉
├── scripts/
│   ├── init_duckdb.py        # 初始化 L2（建预聚合表 + 物化视图）
│   ├── incremental.py        # 季度增量：只扫新分区 → 更新 L2
│   └── export_ladybug.py     # 从 DuckDB 物化 LadybugDB 边表
├── data/                     # 模拟数据（Parquet 分区）
└── output/                   # 六段 JSON + Q1-Q5 明细
```

## 快速开始
```bash
# 1. 生成模拟数据（Parquet 分区）
python -c "from data.gen_sim import main; main()"

# 2. 初始化 DuckDB 温层（建库 + 预聚合表 + 物化视图）
python scripts/init_duckdb.py

# 3. 端到端运行技能（六段输出 + 奇兵拓线 + 五间交叉）
python run_demo.py

# 4.（可选）季度增量：只扫新分区
python scripts/incremental.py --quarter 2024-Q4

# 5.（可选）把关系边物化进 LadybugDB
python scripts/export_ladybug.py
```

## 红线
- 本技能为技术思路探讨，不构成办案指导
- AI 不出定性结论，一切以原始证据与法定程序为准
- 每条推断必须可溯源（挂 source_rows）
```

## 架构收敛（单机版）
```
L1 热   Redis          特征/五间命中/覆盖度        ms
L2 温   DuckDB 单文件   预聚合(主体×月)+物化视图     s
L3 冷   Parquet 分区    原始流水/通话/中标           min
L4 图   LadybugDB      关系多跳/过桥/串并           s
L5 增量 Python脚本      cron 跑新增分区→更新DuckDB
```
