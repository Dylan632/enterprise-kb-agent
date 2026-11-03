# 02 · 方案

## 1. 整体流程

```text
                 ┌──────────────────────────────────────────┐
   用户问题 ───▶ │  Agent 主循环（while true）               │
                 │                                          │
                 │  budget.check()  ── 超限 ──▶ unfinished   │
                 │        │                                 │
                 │        ▼                                 │
                 │   LLM 选择动作（tool_calls）              │
                 │        │                                 │
                 │        ├─ 无 tool_calls ──▶ 校验降级(insufficient)│
                 │        ▼                                 │
                 │   执行工具 → 观察结果 → 追加到 messages    │
                 │        │                                 │
                 │        └─ submit_answer ──▶ 校验 → 结束    │
                 └──────────────────────────────────────────┘
```

退出权在模型：模型调用 `submit_answer` 即结束。`Budget` 只在三种异常下兜底
（步数上限 / 连续相同调用 / token 预算），并返回明确的 `unfinished`。

## 2. 四个工具

| 工具 | 作用 | 为什么需要它 |
| --- | --- | --- |
| `search_docs` | 返回片段 + `score` / `coverage` / `hit_quality` | 让模型自己判断"够不够相关"，而不是硬给 topK |
| `read_chunk` | 返回片段全文 + 文档元信息（版本/生效日/是否被替代） | 引用只能来自原文；元信息支撑冲突判定 |
| `check_conflicts` | 返回冲突组与 `resolvable` 标记 | 把"查证"变成一个可观察的动作 |
| `submit_answer` | 提交结构化答案，唯一正常出口 | 让结局可枚举、可校验，而不是解析自由文本 |

`hit_quality` 是本设计的关键：检索层不假装自己总是对的。覆盖率 < 0.12 就返回 `none`，
把"这次没搜到"这个事实明确交给模型，模型才有机会改写查询而不是硬答。

## 3. 动态决策体现在哪里

真实模型（`examples/`）与离线模拟器（`examples/offline-mock/`）均展示了根据工具返回调整下一步动作的闭环：

1. **检索弱命中 → 改写查询重检（`examples/03-out-of-scope/trace.md`）**：
   - 第 1 轮检索 `年会预算标准` / `会议活动费用预算审批`，工具返回 `hit_quality=weak/none`
   - 第 2 轮模型根据首轮观察，改用制度常见措辞 `员工活动经费 团建费用标准 每人` / `年度庆典 大型活动 经费额度 专项预算` 二次检索
   - 仍无强命中后，第 3 轮提交 `insufficient`（在离线模拟器中，`examples/offline-mock/04-query-rewrite/trace.md` 同样展示了第 1 步 `住酒店一晚上限多少钱？` 返回 `none` → 第 2 步改写为 `出差住宿标准` 命中 `D01#3` 的过程；而真实模型在 Q4 首轮即主动将口语改写为 2 条书面查询并发检索）。
2. **检索发现多版本/跨文档分歧 → 主动补读关系条款与冲突检测（`examples/02-doc-conflict/trace.md`）**：
   - 第 1 轮检索 `报销期限` 同时召回 `D02#2`（15 日）与 `D01#2`（30 日）
   - 第 2 轮模型观察到两份文件并存后，不仅读取了 `D01#2` 和 `D02#2`，还主动补读了 **`D02#1`（与既有制度的关系：「两者并行执行」）** 并调用 `check_conflicts`
   - 第 3 轮根据 `resolvable=false` 的结果并列举证，提交 `need_confirmation`（离线模拟器串行版本见 `examples/offline-mock/02-doc-conflict/trace.md`：第 3 步 `check_conflicts` 后在第 4 步补读 `D01#2`）。
3. **工具超时失败 → 换片段重试 → 拒绝凭摘要作答（`examples/07-tool-failure/trace.md`）**：
   - 第 2 轮读取 `D02#2`、`D01#2`、`D02#1` 均超时失败 → 第 3 轮重试仍失败 → 第 4 轮返回 `error`。

## 4. 冲突的两种类型

```text
同一个 fact.key 出现 ≥2 个不同取值
        │
        ├─ 存在 supersedes / superseded_by 关系？
        │      └─ 是 → resolvable=true，winner=新版本
        │            答案引用新版，并附「依据：D03 替代 D04」
        │
        └─ 否，且多份均 active、作用域相同
               └─ resolvable=false
                     答案并列列出各文件取值 + 出处 + 请用户确认
```

语料里两组冲突分别对应这两种：

| 冲突 | 文件 | 取值 | 结论 |
| --- | --- | --- | --- |
| 报销期限 | D01 v1.2 / D02 v2.0 | 30 日 / 15 日 | 不可判定（D02 声明"并行执行"，无优先级） |
| 年假天数 | D03 v3.0 / D04 v1.0 | 7 天 / 5 天 | 可判定（D03 明确替代 D04） |

## 5. 双层防线

| 层 | 机制 | 拦截什么 |
| --- | --- | --- |
| 模型侧 | 提示词要求提交前调用 `check_conflicts`，按 `resolvable` 分流 | 正常路径 |
| 系统侧 | `verify.apply_guardrails`：引用逐字比对；无有效引用则降级；被引文档间存在未消解冲突则降级 | 模型漏检 |

系统侧不是"不信任模型"的姿态，而是承认**引用正确性属于可机械校验的范畴**，
不该依赖概率。`tests/test_verify.py` 单独覆盖这一层。

## 6. 技术取舍

| 决策 | 选择 | 放弃的方案 | 理由 |
| --- | --- | --- | --- |
| 语言 | Python 单进程 CLI | Java（我原有项目是 Java） | 评审 `python -m src.cli` 一行跑通；Java 需 JDK+Maven，多一层环境失败风险 |
| 检索 | 内存 BM25（纯标准库） | Elasticsearch / 向量库 | ES 要起服务；`pip install` 失败会直接毁掉评审体验。题面允许本地实现 |
| 分词 | 汉字二元组 | jieba | 零依赖；制度类短文本二元组足够，且行为可预测 |
| 版本排序 | BM25 分数 × 状态权重（archived 0.55） | 提示词提醒模型注意版本 | 旧版更短、BM25 长度归一化占优，会稳定压过新版。这是检索层问题，不该让模型擦屁股 |
| Agent 框架 | 手写 40 行循环 | LangGraph / ADK | 题面明说"框架名称不加分"；手写循环让每步的退出条件都可见 |
| 模型调用 | 标准库 urllib | openai SDK | 少一个依赖；OpenAI 兼容协议足够 |
| 事实抽取 | 构建期人工标注 `facts` | 运行时用模型抽 | 冲突判定必须可测试、可复现，不能依赖模型输出 |

## 7. 与本人既有项目的关系

我有两个相关项目：`PaiCLI`（Java CLI Agent，ReAct + 工具注册 + 预算 + Trace）和
`PaiSmart`（Spring Boot 企业 RAG，ES 混合检索 + 引用编号映射）。

**借鉴的是设计，不是代码：**

- `PaiCLI/agent/Agent.java` 的"循环退出交给模型、预算只做兜底" → 本项目的 `agent.py` + `budget.py`
- `PaiCLI/agent/AgentBudget.java` 的停滞检测（连续 N 次相同调用）→ `budget.record_tool_calls`
- `PaiCLI/hitl` 的"需要人工确认"状态 → 简化成 `need_confirmation` 状态返回
- `PaiSmart/service/ChatHandler` 的引用编号映射 → `Citation(doc_id, chunk_id, quote)`
- `PaiSmart/service/HybridSearchService` 的"向量失败退化为纯文本"分支 → 对应本项目的 `hit_quality=none` 分支

**没有搬的：**

- PaiSmart 的 ES + Kafka + MinIO + MySQL + Redis 五件套 —— 评审不可能为一道题起这套环境，且题面要求"可复制的运行命令"
- PaiCLI 的 browser / LSP / MCP / TUI / skill 等 200 余个文件 —— 与本题无关，且会让"个人贡献"无法界定

结论：把两个项目当**零件库**而不是**底座**。最终规模约 1900 行（含注释与文档字符串），
测试 700 余行（56 个用例），每一行都能解释为什么在这。
