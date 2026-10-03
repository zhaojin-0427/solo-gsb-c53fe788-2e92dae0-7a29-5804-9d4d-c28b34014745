# 关系查询溯源与反事实解释 API

客户端一次请求提交 **多张带主键的关系表快照** 和一个 **受限关系代数计划**，
服务执行查询，并为每条输出返回：

* **全部极小来源行见证集**（minimal witness sets / why-provenance）；
* **布尔溯源表达式**（DNF：见证内部为合取 `∧`，见证之间为析取 `∨`）；
* 可选地，对指定输出计算 **令其消失的全部最小源行删除集**，并对每个删除集
  **在删除后的快照上重算整条计划**，返回重算结果。

技术栈：Python 3.11 · FastAPI · SQLite（幂等响应持久化）· 无外部服务依赖。

---

## 1. 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动后：

| 用途 | 地址 |
| --- | --- |
| HTTP API | http://localhost:8000 |
| 交互式文档（Swagger） | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/health |

幂等响应持久化在 Docker 卷 `provenance-data`（容器内 `/data/provenance.db`）。

### 本地直接运行（不使用 Docker）

```bash
python3 -m pip install -r requirements.txt
PROVENANCE_DB=./data/provenance.db \
  python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
python3 -m pip install pytest
python3 -m pytest -q
```

---

## 2. API

### `POST /query`

请求体（JSON）：

```jsonc
{
  "idempotency_key": "readme-demo-1",        // 必填，绑定本次规范化请求
  "tables": [ ... ],                         // 1..n 张表快照（见下）
  "plan": { ... },                           // 1 个关系代数计划
  "explain": { "index": 0 }                  // 可选：对某条输出做反事实解释
}
```

**表快照**

```jsonc
{
  "table": "orders",
  "primary_key": ["oid"],                    // 必填，非空；支持复合主键
  "columns": [                               // 可省略（从行推断）
    {"name": "oid", "type": "int"},          // 类型: int/float/string/bool/any
    {"name": "cid", "type": "int"},
    {"name": "amount", "type": "int"}
  ],
  "rows": [ {"oid": 1, "cid": 10, "amount": 50}, ... ]
}
```

**关系代数计划节点**

| 节点 | 形式 |
| --- | --- |
| 扫描 | `{"type":"scan","table":"orders"}` |
| 选择/过滤 | `{"type":"filter","input":<node>,"predicate":<谓词>}` |
| 投影 | `{"type":"project","input":<node>,"columns":["a",{"from":"b","as":"c"}]}` |
| 等值连接 | `{"type":"join","left":<node>,"right":<node>,"on":[{"left":"cid","right":"cid"}]}` |
| 分组聚合 | `{"type":"aggregate","input":<node>,"group_by":["cid"],"aggregates":[{"fn":"sum","column":"amount","as":"total"}]}` |

* 谓词树：叶子 `{"column":"amount","op":">=","value":40}`；
  算子 `= <> < <= > >=` 以及 `is_null` / `is_not_null`；
  布尔组合 `{"and":[...]}`、`{"or":[...]}`、`{"not":{...}}`。
  谓词按 SQL 三值逻辑求值：与 `NULL` 比较得到 UNKNOWN，不满足选择条件。
* 聚合函数：`count`（`column` 可写 `"*"` 或具体列，后者忽略 NULL）、
  `sum` / `min` / `max`（自动忽略 NULL；全 NULL 时 `sum/min/max` 为 `null`）。
* 等值连接的连接列按 USING 语义合并；其余右表列名冲突时自动限定为
  `right.<列>`（可用 `right_alias` 指定限定名），或在投影中重命名。
* 计划只能引用已提交的表与其字段，否则返回 `ILLEGAL_REFERENCE` / `UNKNOWN_TABLE`。

**反事实目标选择器**（`explain`，二选一）：

* `{"index": 0}` —— 输出按全部列全序排序后的稳定下标；
* `{"row": {...}}` —— 与输出列完全相等的一行。

### 响应约定

* 所有响应都是 **规范化（canonical）JSON**：对象键按码点排序、无多余空白、
  无尾随换行；成功响应附带 `X-Idempotent-Replay: true/false`。
* 幂等：`(idempotency_key, 规范化请求指纹)` 绑定。重复请求 **逐字节一致**；
  同一键但规范化计划/快照/目标不同 → `409 IDEMPOTENCY_KEY_CONFLICT`。
  JSON 中对象键的书写顺序不影响绑定（指纹基于规范化 JSON 计算）。

---

## 3. 实际请求 / 响应示例

### 请求：过滤 + 等值连接 + 投影，并解释输出行 0

```bash
curl -s -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d '{
  "idempotency_key": "readme-demo-1",
  "tables": [
    {"table": "orders", "primary_key": ["oid"],
     "columns": [{"name":"oid","type":"int"},{"name":"cid","type":"int"},{"name":"amount","type":"int"}],
     "rows": [{"oid":1,"cid":10,"amount":50},{"oid":2,"cid":10,"amount":30}]},
    {"table": "customers", "primary_key": ["cid"],
     "columns": [{"name":"cid","type":"int"},{"name":"name","type":"string"}],
     "rows": [{"cid":10,"name":"Alice"}]}
  ],
  "plan": {
    "type": "project",
    "columns": ["name", {"from":"oid","as":"order_id"}, "amount"],
    "input": {
      "type": "filter",
      "predicate": {"column":"amount","op":">=","value":40},
      "input": {
        "type": "join",
        "left": {"type":"scan","table":"orders"},
        "right": {"type":"scan","table":"customers"},
        "on": [{"left":"cid","right":"cid"}]
      }
    }
  },
  "explain": {"index": 0}
}'
```

实际响应（线上传输为单行紧凑 JSON；这里为可读性格式化）：

```json
{
  "status": "ok",
  "symbol_table": [
    {"symbol": "s1", "source": "customers[10]"},
    {"symbol": "s2", "source": "orders[1]"}
  ],
  "result": {
    "columns": ["name", "order_id", "amount"],
    "row_count": 1,
    "rows": [
      {
        "row": {"name": "Alice", "order_id": 1, "amount": 50},
        "witness_count": 1,
        "minimal_witnesses": [
          {
            "labels": ["customers[10]", "orders[1]"],
            "symbols": ["s1", "s2"],
            "conjunction": "s1 ∧ s2"
          }
        ],
        "provenance": {
          "dnf": "(s1 ∧ s2)",
          "dnf_structured": [["s1", "s2"]],
          "labels_structured": [["customers[10]", "orders[1]"]]
        }
      }
    ]
  },
  "counterfactual": {
    "target_index": 0,
    "target": {"name": "Alice", "order_id": 1, "amount": 50},
    "target_provenance": {
      "dnf": "(s1 ∧ s2)",
      "dnf_structured": [["s1", "s2"]],
      "labels_structured": [["customers[10]", "orders[1]"]]
    },
    "minimal_deletion_count": 2,
    "minimal_deletion_sets": [
      ["customers[10]"],
      ["orders[1]"]
    ],
    "scenarios": [
      {
        "delete": ["customers[10]"],
        "target_present": false,
        "result": {"columns": ["name", "order_id", "amount"], "row_count": 0, "rows": []}
      },
      {
        "delete": ["orders[1]"],
        "target_present": false,
        "result": {"columns": ["name", "order_id", "amount"], "row_count": 0, "rows": []}
      }
    ]
  }
}
```

解读：输出行 `(Alice,1,50)` 的唯一极小见证是 `orders[1] ∧ customers[10]`
（两条源行合取）；让它消失的最小删除集有两个：删客户 `customers[10]`
或删订单 `orders[1]`。两种删除都会在重算后使目标消失。

### 分组聚合的数值溯源（count / sum / min / max）

聚合输出行 = 组键 + 各聚合值。每个聚合表达式都为“这一确切数值”贡献一个
见证组件，与“组至少剩一行”的存在性组件合取（逐组件取并集）后极小化：

* `count(*)` / `count(col)`：覆盖 **所有参与计数的行**（后者忽略 NULL）
  —— 一个合取；
* `sum(col)`：枚举 **和为目标值的全部极小非空行组合**（按行而非按值，同值行
  产生不同组合并完整保留；**空组合永远不是见证，即使目标和为 0**）；
* `min(col)` / `max(col)`：枚举 **仍能产生目标极值的极小组合** —— 每个
  等于极值的行各成一个见证。

因此同组订单金额为 50 和 30 时，`count=2`、`sum=80` 只在两行都在时成立，
溯源是单个合取 `s1 ∧ s2`（而不是两条单行见证的析取）；删任一行数值即变，
最小删除集是两个单例，与删除后重算完全一致。

### 请求：分组聚合 + 解释组 `cid=10`

```bash
curl -s -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d '{
  "idempotency_key": "readme-agg-1",
  "tables": [
    {"table": "orders", "primary_key": ["oid"],
     "columns": [{"name":"oid","type":"int"},{"name":"cid","type":"int"},{"name":"amount","type":"int"}],
     "rows": [
       {"oid":1,"cid":10,"amount":50},
       {"oid":2,"cid":10,"amount":30},
       {"oid":3,"cid":20,"amount":10}
     ]}
  ],
  "plan": {
    "type": "aggregate",
    "input": {"type":"scan","table":"orders"},
    "group_by": ["cid"],
    "aggregates": [
      {"fn":"count","column":"*","as":"n"},
      {"fn":"sum","column":"amount","as":"total"}
    ]
  },
  "explain": {"row": {"cid":10,"n":2,"total":80}}
}'
```

实际响应（线上传输为单行紧凑 JSON；这里为可读性格式化）：

```json
{
  "status": "ok",
  "symbol_table": [
    {"symbol": "s1", "source": "orders[1]"},
    {"symbol": "s2", "source": "orders[2]"},
    {"symbol": "s3", "source": "orders[3]"}
  ],
  "result": {
    "columns": ["cid", "n", "total"],
    "row_count": 2,
    "rows": [
      {
        "row": {"cid": 10, "n": 2, "total": 80},
        "witness_count": 1,
        "minimal_witnesses": [
          {
            "labels": ["orders[1]", "orders[2]"],
            "symbols": ["s1", "s2"],
            "conjunction": "s1 ∧ s2"
          }
        ],
        "provenance": {
          "dnf": "(s1 ∧ s2)",
          "dnf_structured": [["s1", "s2"]],
          "labels_structured": [["orders[1]", "orders[2]"]]
        }
      },
      {
        "row": {"cid": 20, "n": 1, "total": 10},
        "witness_count": 1,
        "minimal_witnesses": [
          {"labels": ["orders[3]"], "symbols": ["s3"], "conjunction": "s3"}
        ],
        "provenance": {
          "dnf": "(s3)",
          "dnf_structured": [["s3"]],
          "labels_structured": [["orders[3]"]]
        }
      }
    ]
  },
  "counterfactual": {
    "target_index": 0,
    "target": {"cid": 10, "n": 2, "total": 80},
    "target_provenance": {
      "dnf": "(s1 ∧ s2)",
      "dnf_structured": [["s1", "s2"]],
      "labels_structured": [["orders[1]", "orders[2]"]]
    },
    "minimal_deletion_count": 2,
    "minimal_deletion_sets": [
      ["orders[1]"],
      ["orders[2]"]
    ],
    "scenarios": [
      {
        "delete": ["orders[1]"],
        "target_present": false,
        "result": {
          "columns": ["cid", "n", "total"],
          "row_count": 2,
          "rows": [
            {"cid": 10, "n": 1, "total": 30},
            {"cid": 20, "n": 1, "total": 10}
          ]
        }
      },
      {
        "delete": ["orders[2]"],
        "target_present": false,
        "result": {
          "columns": ["cid", "n", "total"],
          "row_count": 2,
          "rows": [
            {"cid": 10, "n": 1, "total": 50},
            {"cid": 20, "n": 1, "total": 10}
          ]
        }
      }
    ]
  }
}
```

解读：目标行 `(10,2,80)` 的唯一极小见证是 `orders[1] ∧ orders[2]`；
令它消失的最小删除集是删 `orders[1]` 或 `orders[2]`。两个场景都在删除后
重算了整条计划：组 `cid=10` 仍在，但变成 `(10,1,30)` / `(10,1,50)`，
目标行消失（`target_present: false`）——布尔溯源、最小删除集与重算结果
三者一致。

多见证（DNF 多析取项）在 `sum`/`min`/`max` 出现多种极小组合时产生。例如
同组金额为 `30, 30, 60, -60` 时 `sum=60` 的极小组合是 `{30,30}` 与 `{60}`，
溯源为 `(s1 ∧ s2) ∨ (s3)`。

### 混合正负金额的 `sum`：以删除后重算判定极小删除集

`sum` 对源行 **不是单调的**：同组金额同时含正数与负数时，删一行可能让总和
离开、甚至重新落回目标值，因此见证的极小命中集只能给出单调近似（会漏报或
多报）。此时服务改为 **按删除后在快照上完整重算整条计划** 来判定：枚举源行
删除候选（按基数递增），凡是已经包含某个更小命中方案的超集直接剪枝，最后
返回全部按包含关系极小、且重算后目标行确实消失的删除集；每个方案都附带
重算场景。方案与场景均按来源标签全序稳定排序。`count`/`min`/`max` 以及
同号（全非负或全非正）的 `sum` 仍走命中集路径，语义不变。

#### 请求：金额 `5, 2, -2`，解释组 `cid=10`（`sum=5`）

```bash
curl -s -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d '{
  "idempotency_key": "readme-mixed-sum-1",
  "tables": [
    {"table": "orders", "primary_key": ["oid"],
     "columns": [{"name":"oid","type":"int"},{"name":"cid","type":"int"},{"name":"amount","type":"int"}],
     "rows": [
       {"oid":1,"cid":10,"amount":5},
       {"oid":2,"cid":10,"amount":2},
       {"oid":3,"cid":10,"amount":-2},
       {"oid":4,"cid":20,"amount":100}
     ]}
  ],
  "plan": {
    "type": "aggregate",
    "input": {"type": "scan", "table": "orders"},
    "group_by": ["cid"],
    "aggregates": [{"fn":"sum","column":"amount","as":"total"}]
  },
  "explain": {"row": {"cid":10,"total":5}}
}'
```

实际响应（线上传输为单行紧凑 JSON；这里为可读性格式化）：

```json
{
  "status": "ok",
  "symbol_table": [
    {"symbol": "s1", "source": "orders[1]"},
    {"symbol": "s2", "source": "orders[4]"}
  ],
  "result": {
    "columns": ["cid", "total"],
    "row_count": 2,
    "rows": [
      {
        "row": {"cid": 10, "total": 5},
        "witness_count": 1,
        "minimal_witnesses": [
          {"labels": ["orders[1]"], "symbols": ["s1"], "conjunction": "s1"}
        ],
        "provenance": {
          "dnf": "(s1)",
          "dnf_structured": [["s1"]],
          "labels_structured": [["orders[1]"]]
        }
      },
      {
        "row": {"cid": 20, "total": 100},
        "witness_count": 1,
        "minimal_witnesses": [
          {"labels": ["orders[4]"], "symbols": ["s2"], "conjunction": "s2"}
        ],
        "provenance": {
          "dnf": "(s2)",
          "dnf_structured": [["s2"]],
          "labels_structured": [["orders[4]"]]
        }
      }
    ]
  },
  "counterfactual": {
    "target_index": 0,
    "target": {"cid": 10, "total": 5},
    "target_provenance": {
      "dnf": "(s1)",
      "dnf_structured": [["s1"]],
      "labels_structured": [["orders[1]"]]
    },
    "minimal_deletion_count": 3,
    "minimal_deletion_sets": [
      ["orders[1]"],
      ["orders[2]"],
      ["orders[3]"]
    ],
    "scenarios": [
      {
        "delete": ["orders[1]"],
        "target_present": false,
        "result": {
          "columns": ["cid", "total"],
          "row_count": 2,
          "rows": [
            {"cid": 10, "total": 0},
            {"cid": 20, "total": 100}
          ]
        }
      },
      {
        "delete": ["orders[2]"],
        "target_present": false,
        "result": {
          "columns": ["cid", "total"],
          "row_count": 2,
          "rows": [
            {"cid": 10, "total": 3},
            {"cid": 20, "total": 100}
          ]
        }
      },
      {
        "delete": ["orders[3]"],
        "target_present": false,
        "result": {
          "columns": ["cid", "total"],
          "row_count": 2,
          "rows": [
            {"cid": 10, "total": 7},
            {"cid": 20, "total": 100}
          ]
        }
      }
    ]
  }
}
```

解读：命中集近似只会给出 `orders[1]`（唯一见证 `s1`），但重算判定补上了
**单独删除 `orders[2]`（总和变 3）或 `orders[3]`（总和变 7）** 这两个被漏报
的极小方案；三者都使目标行 `(10,5)` 消失，且互不包含，因此全部返回（按
标签排序）。目标行的见证仍只有 `s1`：`2 + (-2) = 0` 的组合在见证极小化时
被 `{5}` 支配，不进入 DNF。

#### 目标和为 0：空组合不再充当见证

同组金额为 `5, -5`、`sum=0` 时，空子集不能被当作见证；极小见证是非空组合
`orders[1] ∧ orders[2]`，极小删除集为 `["orders[1]"]` 与 `["orders[2]"]`，
重算后总和分别为 `-5` / `5`，目标行 `{"total": 0}` 均消失。此前空组合会与
“组存在性”合取后被误极小化，导致反事实误报需要同时删除两行。

### 幂等重放逐字节一致

```bash
curl -s ... -d @req.json > a.json
curl -s ... -d @req.json > b.json
cmp a.json b.json && echo "byte-identical"   # byte-identical
# 第二次响应头: X-Idempotent-Replay: true
```

---

## 4. 结构化错误

错误统一为 `4xx` + 规范化 JSON：

```json
{"error": {"code": "DUPLICATE_PRIMARY_KEY",
           "message": "Table 't': duplicate primary key [1]",
           "details": {"table": "t", "primary_key": [1]}}}
```

| code | 触发条件 |
| --- | --- |
| `MALFORMED_REQUEST` | JSON 非法、请求不符合 schema、行含未声明列、表重名等 |
| `MALFORMED_PLAN` | 节点类型未知、谓词结构非法、投影/聚合别名冲突等 |
| `UNKNOWN_TABLE` | 计划引用未提交的表 |
| `ILLEGAL_REFERENCE` | 计划/谓词/分组/聚合引用不存在的字段 |
| `DUPLICATE_PRIMARY_KEY` | 同一表快照内主键重复 |
| `NULL_PRIMARY_KEY` | 主键的任一分量为 null |
| `EMPTY_AGGREGATE` | 聚合输入为空（无法形成任何组；含标量聚合） |
| `TYPE_ERROR` | 声明类型不符、跨类型比较/连接、对非数值求 `sum`、对布尔值做有序比较 |
| `TARGET_NOT_FOUND` | `explain` 的 index 越界或 row 不匹配任何输出 |
| `IDEMPOTENCY_KEY_CONFLICT` | 幂等键已绑定到另一个规范化请求（HTTP 409） |

---

## 5. 溯源语义说明

* 来源行标签为 `表名[主键值,...]`，复合主键按顺序拼接。符号表把标签稳定映射为
  `s1, s2, ...`（按表名、主键全序排序）。
* 见证集经过包含极小化：投影去列、自连接等造成的冗余派生会被剔除；
  同一标签集合只保留一份，输出按标签全序稳定排序。
* 选择、等值连接、投影对源行单调；`count`/`min`/`max`（以及同号 `sum`）
  聚合输出的见证把“组存在性”（至少一名成员存活）与每个聚合表达式的数值
  组件合取：`count` 覆盖所有参与计数的行，`sum`/`min`/`max` 枚举产生目标
  值的极小 **非空** 行组合。这些情形下“令输出消失的最小删除集”等于该输出
  全部极小见证的 **极小命中集**（hypergraph transversal）。
* **混合正负值的 `sum` 不单调**（删行可让总和离开或重新落回目标值），命中
  集只是单调近似。检测到目标组的某列同时含正数与负数时，服务改为枚举源行
  删除候选并 **重算整条计划**，按重算后目标行是否消失判定，返回全部按包含
  关系极小的删除集（超集剪枝；方案与场景按来源标签稳定排序）。NULL 行继续
  忽略、不进入符号表；多组场景下只追踪目标组的来源行；删除使聚合输入为空时
  场景保持既有的结构化重算错误（`EMPTY_AGGREGATE`）。
