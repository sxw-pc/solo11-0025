# 口径校对台（Metric Semantics Checker）

**一句话定位：** 把散落在文档和报表 SQL 里的指标口径收敛为声明式字典，并对本地 SQL 做静态解析，核对报表是否仍在用同一套口径。

## 它解决什么问题

小团队里「活跃用户」「成交额」等指标会同时出现在多份报表中。指标名相同、算法却不同，月底对账才发现口径已经漂移。本工具把指标定义写进 YAML，静态抽取 SQL 报表中的表、字段、聚合、过滤和粒度，按固定的四类结论（派生比率另有「无法判定」增量类别）输出可归档报告：

```text
口径字典 ──校验/指纹──┐
                       ├── 结构化比对 ── consistent / drift / missing_field / unregistered
                       │                  （派生比率另有 undetermined）
SQL 报表 ──静态解析────┘
```

## 合规与边界声明

- **纯离线运行。** 运行期不访问任何外部 API、CDN、云服务或远程数据库；仓库不包含真实密钥、账号、公司名或外部域名。
- **不连接数据库，不执行 SQL。** 只做词法/语法静态解析；无法判断运行期数据内容或查询代价。
- **本地只读查看。** HTTP 服务只允许绑定 `127.0.0.1`、`localhost` 或 `::1`，只提供 GET/HEAD；没有写入、登录或管理端点。
- **合成数据。** `fixtures/` 中的指标、库表、人员与报表名均为演示用虚构名称。
- **扫描范围有限。** SQL 解析只覆盖常见 `SELECT` / 聚合 / `WHERE` / `GROUP BY` 子集，不做数据血缘或自然语言口径理解。

## 快速上手

要求 Python 3.10+。核心运行不依赖第三方库；`sqlglot` 与 `PyYAML` 是可选增强。

```bash
# 1. 安装测试依赖（不安装也能用降级解析链路）
python -m pip install pytest

# 2. 一条命令完成核对：解析、比对、落库、生成三份报告
python -m koujing check fixtures/reports

# 3. 启动本地只读查看页（5 秒内可访问）
python -m koujing serve
# 浏览器打开 http://127.0.0.1:8765/

# 4. 跑测试（无网络）
pytest -q

# 可选：安装增强解析器
python -m pip install sqlglot PyYAML
```

示例批次默认包含 6 份报表，结论为 `consistent=3 drift=1 missing_field=1 unregistered=1`。报告写入 `out/<batch_id>/report.{md,json,html}`，SQLite 写入 `data/koujing.db`。

### CI 卡点

按 F8，`check` **默认不因业务结论失败**，便于观察和归档。需要阻断时显式指定类别：

```bash
python -m koujing check fixtures/reports --fail-on drift,missing_field,unregistered
# 退出码 1；报告已经生成并落库
```

其他常用命令：

```bash
python -m koujing history --limit 10
python -m koujing export <batch_id>
python -m koujing check fixtures/reports --parser fallback --quiet
python -m koujing check fixtures/reports --dict fixtures/dict/metrics_min.yaml
```

## 口径字典

默认字典为 `fixtures/dict/metrics.yaml`，也支持 JSON。每项指标字段：

| 字段 | 必填 | 说明 |
| --- | :---: | --- |
| `metric` | 是 | 指标唯一名，会规范为小写 |
| `owner` | 是 | 口径责任人/团队标识 |
| `description` | 是 | 人类可读的定义说明 |
| `source_tables` | 是 | 来源表列表（非空） |
| `source_fields` | 是 | 口径依赖字段列表（非空） |
| `aggregation` | 是 | `sum` / `count` / `count_distinct` / `avg` / `max` / `min`；派生比率指标填 `ratio` |
| `filter_expr` | 否 | 规范化过滤表达式，可为空 |
| `granularity` | 是 | 报表时间粒度，如 `day` / `week` / `month`；无分组为 `none` |
| `version` | 是 | 口径版本号（字符串） |
| `ratio` | 否 | 派生比率的分子/分母契约；声明后 `aggregation` 归一为 `ratio` |

最小样例：

```yaml
metrics:
  - metric: active_user
    owner: growth_analytics
    description: 登录成功且状态正常的去重用户数
    source_tables: [fact_login]
    source_fields: [user_id, login_at, event_type, status]
    aggregation: count_distinct
    filter_expr: "event_type = 'login' and status = 'ok'"
    granularity: day
    version: 1.0.0
```

校验会一次性返回结构化错误列表（`missing_field`、`duplicate_metric`、`invalid_aggregation`、`empty_source_fields`、`invalid_ratio_operand`、`invalid_ratio_kind`、`invalid_zero_division`、`unsupported_ratio_aggregation` 等），CLI 以退出码 2 结束。字典经字段小写化、列表排序去重、表达式空白规范化后计算 `sha256` 指纹，用于批次追溯和字典快照。

### 派生比率契约

单聚合指标维持原有字段与指纹不变（`ratio` 键只在声明比率时出现）。派生比率指标新增 `ratio` 块：

```yaml
  - metric: avg_order_value
    owner: trade_operations
    description: 客单价 = 成交金额合计 / 去重订单数
    source_tables: [fact_order]
    source_fields: [pay_amount, order_id, order_paid_at, order_status]
    aggregation: ratio
    filter_expr: "order_status = 'paid'"
    granularity: day
    version: 1.0.0
    ratio:
      kind: population_ratio          # population_ratio | average_ratio
      numerator:  {aggregation: sum, field: pay_amount}
      denominator: {aggregation: count_distinct, field: order_id}
      zero_division: nullif           # nullif | coalesce_zero | unprotected
```

- `kind: population_ratio` 对应 `SUM(x) / NULLIF(COUNT(y), 0)` 一类**总体比率**：分子、分母各自聚合后再相除。
- `kind: average_ratio` 对应 `AVG(x / NULLIF(y, 0))` 一类**平均比率**：逐行形成比率后再平均；分子/分母操作数的 `aggregation` 用 `avg` 表示逐行字段。两者即使字段相同也绝不视为同一口径。
- 分子分母对调、平均比率与总体比率混用、零分母策略不一致，都会落到 `ratio.operand_order`、`ratio.kind`、`ratio.zero_division` 等具体差异字段。
- 报表侧分母（或分子）出现解析器无法处理的表达式（子查询、未知函数、多重除号）时，该派生的聚合别名仍保留为候选，结论为新增的 `undetermined`（无法判定），并给出 `ratio.undetermined_reason`，不会静默吞掉或误报一致。
- `undetermined` 是增量类别：没有比率无法判定项时，报告与批次的 `summary` 仍只含原四类，计数与历史 fixtures 完全不变；出现时额外提供 `extra_summary` / `extra_groups`。`--fail-on` 同样接受 `undetermined`。

样例见 `fixtures/dict/metrics_ratio.yaml` 与 `fixtures/ratio_reports/`（2 一致 / 3 偏差 / 1 无法判定）。

## 四类结论

| 结论 | 触发条件 | 反例（不会命中） |
| --- | --- | --- |
| `consistent` | 报表的 `source_tables`、`source_fields`、`aggregation`、`filter_expr`、`granularity` 与字典全部一致 | 只差一个过滤条件 |
| `drift` | 已登记指标但在聚合方式、过滤表达式、来源表或粒度上不一致 | 字典未登记的别名 |
| `missing_field` | 报表引用了字典声明之外的字段，或字典声明字段报表未引用 | 字段集合完全一致 |
| `unregistered` | 报表聚合结果别名未在字典中登记 | 别名命中已登记指标 |
| `undetermined` | 派生比率的分子或分母超出可静态解析子集（仅比率指标） | 分子分母均可归约为受支持聚合 |

判定优先级：**未登记 > 无法判定 > 缺失字段 > 偏差 > 一致**。`undetermined` 只对派生比率产生；纯单聚合批次的结论计数与之前完全一致。一条 finding 的证据包含报表相对路径、起止行号、解析器来源、期望/实际结构、差异明细和解析警告。结论按固定键排序（报表路径、类别、指标、行号），同等输入重复运行 deep-equal。

## 双解析器策略

1. **优先 `sqlglot`**：成功时从 AST 抽取表、聚合、列、过滤、分组，输出统一的 `ParsedQuery`，来源标记为 `primary`。
2. **自动降级**：`sqlglot` 未安装、抛解析异常或崩溃时，自动切到标准库词法器 + 正则解析器，来源标记为 `fallback`，并把原因写入 `warnings`。
3. **强制降级**：`--parser fallback` 用于验证无第三方依赖链路。
4. **部分解析不崩溃**：`fixtures/reports/weird_syntax.sql` 带不支持的 `WINDOW` / `EMIT` 尾子句，降级解析器仍提取核心字段并报告警告。

两套解析器在共同支持的语法子集上归一化为同一结构；验收脚本会验证结论集合一致、报告中的解析器来源随环境变化。

## 输出与存储

一次 `check` 生成：

- `report.md`：按四类（+ 出现时的 `undetermined`）分组的可读报告；
- `report.json`：机读结构，包含 `schema_version`、批次元数据、汇总和逐条证据；
- `report.html`：单文件、内联样式、无外部资源的可归档页面。

SQLite 表结构由 `store.py` 幂等创建：

- `batch`：批次号、UTC 时间、字典指纹、报表数、各结论计数、解析器模式；
- `finding`：批次、报表、类别、指标、行号、证据 JSON、差异 JSON；
- `dict_snapshot`：指纹、字典原文、来源路径。

批次号格式为 `UTC时间戳（精确到微秒）-字典指纹前8位`。写入在单事务内完成；重复批次号写入幂等。所有查询使用参数化绑定。

## 本地查看页

`serve` 仅监听本机回环地址，拒绝非本地 `Host` 与含 `..` 的越权路径，并写入内存审计记录和服务日志。只读端点：

```text
GET /                                       静态查看页
GET /api/health                             健康检查
GET /api/batches                            最近批次
GET /api/batches/<batch_id>                 批次分组视图
GET /api/batches/<batch_id>/findings/<n>    单条证据（从 1 开始）
GET /api/batches/<batch_id>/report.json     下载机读报告
```

页面完全本地化，无 CDN、字体或远程脚本；无写操作端点。

## 目录结构

```text
koujing/
  cli.py / __main__.py       CLI：check / history / export / serve
  config.py                  路径、常量、类别与安全工具
  dict_load.py               字典加载、校验、规范化和指纹
  yaml_lite.py               零依赖 YAML 子集解析器
  sql_parse/                 主解析器、降级解析器、统一模型与词法工具
  compare.py                 结构比对、四类结论与派生比率比对
  report.py                  Markdown / JSON / HTML 渲染
  store.py                   SQLite 建表、迁移与查询
  server.py                  回环只读 HTTP 服务
  static/                    页面、原生 JS、本地 CSS
fixtures/dict/               合成口径字典（含 metrics_ratio.yaml）
fixtures/reports/            覆盖四类结论的 SQL 样例
fixtures/ratio_reports/      派生比率样例（一致/对调/策略/无法判定）
tests/                       无网络单元与集成测试
```

## 退出码

| 码 | 含义 |
| ---: | --- |
| 0 | 完成；或指定 `--fail-on` 类别均未出现 |
| 1 | 完成，但命中了 `--fail-on` 指定的类别；`export` 批次不存在 |
| 2 | 参数、目录、字典校验或读取错误 |

## 测试覆盖

`pytest -q` 可在仅安装 pytest 的环境中通过；需要 `sqlglot` 的结构一致性用例会自动跳过，降级路径始终会被测试（包括模拟 `sqlglot` / PyYAML 缺失）。覆盖：

- 字典每类校验错误、YAML/JSON、降级解析与指纹稳定性；
- 两套解析器的共同结构、`COUNT(DISTINCT)`、注释/字符串、坏语法；
- 四类结论正反例、判定优先级、确定性排序、证据字段；
- 派生比率契约：分子分母抽取、总体/平均比率区分、零分母策略、无法判定；
- 三格式报告、JSON 结构、HTML 自包含、路径防穿越；
- SQLite 幂等写入、批次/类别/指标历史查询、可注入时钟；
- CLI 默认退出码、`--fail-on`、`--quiet`、history/export、模块入口；
- HTTP 只读、下载、非法 Host、路径穿越、方法拒绝与审计。

## 已知限制

- 静态解析无法理解视图展开、动态 SQL、宏、临时表或运行期数据含义。
- 降级解析器只覆盖简单 `SELECT`、直接聚合、常见连接与 `WHERE`/`GROUP BY`；嵌套查询、窗口函数和复杂方言只能给警告，不应作为完整 SQL 语义判断。
- 表达式比较是规范化后的字符串比较，不做布尔等价变换（例如不会把 `a AND b` 与 `b AND a` 判为一致）。
- 指标候选来自聚合结果别名；无别名聚合不会成为候选。
- 报表字段按「聚合参数 + 过滤 + 分组」聚合到指标维度；同一报表多个别名共享过滤/分组时，无法做列级 lineage 归因。
- 字典只读加载，不提供在线编辑、版本合并或审批流。

## 明确不实现

- 不连接真实数据库、不执行 SQL、不做执行计划分析；
- 不做数据血缘图可视化、不做跨报表字段级 lineage；
- 不做 BI 看板、图表或定时调度；
- 不做用户体系、登录或权限；
- 不做字典在线编辑、版本合并；
- 不做自然语言口径解析；
- 不做多 SQL 方言全覆盖。
