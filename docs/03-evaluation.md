# 03 · 评估

## 1. 测试策略

分三层，各管各的事，不互相替代：

| 层 | 命令 | 覆盖对象 | 能否证明"模型决策质量" |
| --- | --- | --- | --- |
| 单元测试 | `pytest` | 检索排序、预算、冲突判定、引用校验、密钥自检 | 不涉及 |
| 离线端到端 | `eval --offline` | 决策链结构、Trace 完整性、降级逻辑 | **不能**，只验证工程链路 |
| 真实模型 | `eval`（需 API key） | 模型在提示词约束下的实际行为 | 能 |

主证据是第三层。第二层保留为对照组，用来区分"工程 bug"和"模型行为差异"——
调试时这个区分很关键，见第 4 节。

## 2. 单元测试

`python -m pytest -q` → **34 passed in 0.18s**

| 文件 | 用例数 | 覆盖内容 |
| --- | --- | --- |
| `test_index.py` | 6 | 分词、strong/weak/none 判定、旧版降权、片段元信息 |
| `test_budget.py` | 6 | 步数上限、停滞检测、token 预算、非法配置 |
| `test_conflict.py` | 4 | 两类冲突的判定差异、按主题过滤 |
| `test_verify.py` | 6 | 引用逐字校验、doc_id 错配、无引用降级、冲突兜底 |
| `test_agent.py` | 9 | 7 条决策路径 + Trace 字段完整性 + 步数上限 |
| `test_no_secrets.py` | 3 | 全仓库密钥扫描、`.env.example` 留空、`.gitignore` 规则 |

## 3. 端到端验收结果

### 3.1 真实模型（主证据）

`python -m src.cli eval`，模型 `deepseek-flash`（DeepSeek-V4.1-Flash）→ **7/7 通过**

| 用例 | 场景 | 期望 | 实际 | 轮次 | 调用 | 引用 | 结果 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Q1 | 明确答案 | answered | answered | 3 | 5 | D01 | PASS |
| Q2 | 文档冲突 | need_confirmation | need_confirmation | 3 | 7 | D01, D02 | PASS |
| Q3 | 资料外问题 | insufficient | insufficient | 4 | 6 | — | PASS |
| Q4 | 改写后的相似问题 | answered | answered | 3 | 5 | D01 | PASS |
| Q5 | 版本冲突 | answered | answered | 3 | 7 | D03, D04 | PASS |
| Q6 | 上下文追问 | answered | answered | 4 | 6 | D03 | PASS |
| Q7 | 工具失败 | error | error | 4 | 9 | — | PASS |

> 轮次 = 模型回复次数；调用 = 工具调用次数。一轮可并发多个工具，故两者不等。

汇总：`examples/summary.json`；逐条 trace：`examples/<case>/trace.{json,md}`。

### 3.2 离线模拟器（对照组）

`python -m src.cli eval --offline` → 7/7 通过，结果在 `examples/offline-mock/`。
与 live 的差异集中在两处：

| 维度 | live（真实模型） | offline（规则模拟） |
| --- | --- | --- |
| 检索策略 | 并发多条不同措辞（Q1 首轮 2 条 query） | 单条 query，失败才改写 |
| 冲突举证 | Q5 额外读 D04#2 版本说明作为"已废止"引用 | 只引新版正文 |
| 平均轮次 | 3.4 | 4.3 |

live 反而**用更少的轮次拿到更完整的举证**，因为它会并发。这也说明规则模拟器的
"逐条串行"策略不是最优解——这一条只能靠真实模型发现。

### 3.3 逐条对照（可点开核对）

**Q1 明确答案** — `examples/01-normal-answer/trace.md`

- 首轮并发 2 条 query（`出差住宿标准 一线城市` / `差旅费用报销 住宿费限额`），最佳 D01#3，coverage 0.75
- 第 2 轮读 D01#3，同时补检 D02（费用报销实施细则）是否有住宿条款
- 答案含 500 元/晚，引用逐字比对通过

**Q2 文档冲突** — `examples/02-doc-conflict/trace.md`

- 7 次调用：2 检索 → 3 读原文（D02#2 / D01#2 / **D02#1**）→ 1 查冲突 → 提交
- 注意它读了 **D02#1「与既有制度的关系」**——那一句正是"两者并行执行"的出处。
  模型自己去核对了"有没有优先级声明"，这是提示词没教的动作。
- 状态 `need_confirmation`，并列 30 日 / 15 日，未擅自选一个

**Q3 资料外问题** — `examples/03-out-of-scope/trace.md`

- 4 轮共 6 次检索，逐步改写：`年会预算` → `员工活动经费标准` → `会议费 活动费 预算审批` → `员工活动、年会、团建费用报销`
- 全部 `hit_quality=weak` 或空，最终 `insufficient`，引用为空
- 答案里列了检索过的关键词，说明"我找过但没有"——比单纯说"不知道"更有用

**Q4 改写后的相似问题** — `examples/04-query-rewrite/trace.md`

- `住酒店一晚上限多少钱？` 首轮无 strong 命中
- 改写后命中 D01#3，5 次调用完成
- 改写动作在 trace 中可见（两条不同 query）

**Q5 版本冲突** — `examples/05-version-conflict/trace.md`

- 引用 **D03#1（7 天）+ D04#2（版本说明）**
- 这个组合是对的：D04#2 支撑"旧版已废止"这句话，D03#1 支撑"7 天"
- 未引用 D04#1（5 天）作为天数依据

**Q6 上下文追问** — `examples/06-followup/trace.md`

- 单看 `那需要提前几天申请？` 无法定位主题
- 检索词被扩展为含"年假"的组合，命中 D03#1
- 答案：`年假需提前 3 个工作日在 HR 系统中提交申请`

**Q7 工具失败** — `examples/07-tool-failure/trace.md`

- 9 次调用，`read_chunk` 全部超时，它换着 chunk 重试了多次
- 最终 `error`，引用为空，明确说明无法核实原文
- **没有**退回用 search 的 40 字片段作答

## 4. 调试过程中修掉的问题

留档是因为这几处都是"看起来能跑、其实结论错"的类型：

| 问题 | 现象 | 定位 | 修复 |
| --- | --- | --- | --- |
| 工具返回结构读错 | 离线模式下所有用例都判"检索无结果" | 工具消息是扁平 dict，代码却按 `payload.hits` 取值 | 改为直接读 `hits`，`test_agent.py` 补回归 |
| 旧版文档排序压过新版 | Q5/Q6 引用 D04（5 天）而非 D03（7 天） | 旧版更短，BM25 长度归一化占优 | 加 `STATUS_WEIGHT`（archived 0.55），`test_index.py` 断言排序 |
| 故障注入未接线 | Q7 期望 error 实得 need_confirmation | `eval` 只读命令行 `--fail-tool`，没读用例里的 `fail_tools` | `build_agent` 支持 `extra_fail`，每用例独立实例 |
| 冲突相关性判定过宽 | live 下 Q2 的 `check_conflicts` 返回 2 组，含无关的"年假天数" | `conflicts_for_query` 用任意 token 重叠，`天数` 命中了 `年假天数` | 改为"键名前两字出现在查询中"或"≥2 token 重叠" |
| 断言写得比模型还粗 | live 下 Q5 判 FAIL（"错误引用 D04"） | 用例禁止引用整个 D04，但引用 D04#2 版本说明是**正确**举证 | 改为 `forbid_chunks: ["D04#1"]`，只禁天数条款 |

最后一条值得单独说：**这次是模型的行为比我的评估标准更合理**。
如果我为了让它"通过"而放宽成"随便引 D04 都行"，就丢掉了一个真实的正确性信号；
正确做法是把断言写细——禁的是"用 5 天当答案"，不是"提到旧版"。

## 5. 失败与限制

| 限制 | 影响 | 现状 |
| --- | --- | --- |
| 冲突依赖预抽取 `facts` | 只能发现已标注字段的冲突 | 语料 11 条事实；真实场景需抽取模型 |
| 冲突相关性是启发式 | 措辞差异大时可能漏检或误带 | 见第 4 节最后一条，已收紧但仍非语义匹配 |
| BM25 无语义 | 同义改写必须靠模型 | Q4 验证了成功路径；失败路径会保守拒答 |
| 追问只做一层指代合并 | 多轮跳转、指代链会失效 | Q6 覆盖单层场景 |
| 无权限过滤 | 所有文档对所有人可见 | 题面未要求 |
| 单次运行成本 | 7 组用例约 40 次模型调用 | 未做成本统计与缓存 |

## 6. 复现方式

```bash
pip install -r requirements.txt
python -m pytest -q                              # 34 passed

# 离线（无需 key）
python -m src.cli eval --offline

# 真实模型
export KB_AGENT_API_KEY=...
python -m src.cli eval

# 单题
python -m src.cli ask "报销期限是多久？" -v
```
