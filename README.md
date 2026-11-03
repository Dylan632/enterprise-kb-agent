# 企业知识问答 Agent

> 小影校招 AI Agent 开发岗位 · **项目题 3**
> 一个可运行、可复核的企业制度问答 Agent：模型自主检索、改写查询、读取原文、检测冲突，并在资料不足或冲突无法判定时明确拒绝作答。

**交付信息**

| 项 | 内容 |
| --- | --- |
| 题号 | 项目题 3 · 企业知识问答 Agent |
| 技术栈 | Python 3.10+，零运行时依赖；未使用 Agent 框架（循环手写，见 `docs/02-solution.md`） |
| 运行方式 | CLI（`python -m src.cli ask/eval`） |
| 实际用时 | 约 100 分钟（含方案设计、AI 辅助编码、三轮实测修复与候选人本人全量代码与 Trace 复核） |
| 未完成部分 | 见第 10 节 |

## 1. 快速开始（评审入口）

环境要求：**Python 3.10+**，无其他运行时依赖（BM25、HTTP 客户端均为标准库实现）。

```bash
# 1) 进入项目目录
cd enterprise-kb-agent

# 2) 安装测试依赖（可选，仅跑测试需要）
pip install -r requirements.txt

# 3) 直接运行，无需 API key —— 默认走离线模拟器
python -m src.cli ask "报销期限是多久？" --offline --verbose

# 4) 跑全部 7 组验收用例，产出 examples/<case>/trace.{json,md}
python -m src.cli eval --offline

# 5) 跑测试
python -m pytest -q
```

接入真实模型（可选，`examples/` 中的结果即由该配置产出）：

```bash
export KB_AGENT_API_KEY=sk-xxx          # 只放环境变量，不要写进文件
export KB_AGENT_BASE_URL=https://api.deepseek.com/v1
export KB_AGENT_MODEL=deepseek-flash
python -m src.cli ask "报销期限是多久？" --verbose
```

> 未设置 `KB_AGENT_API_KEY` 时会自动回落到离线模拟器，并在输出中标注 `offline:scripted`。

## 2. 目录结构

```text
enterprise-kb-agent/
├── README.md
├── requirements.txt
├── .env.example
├── data/
│   ├── docs.json              # 10 篇合成制度（29 个片段 / 11 条结构化事实），含 2 组刻意埋设的冲突
│   └── eval_questions.json    # 7 组验收用例，逐条对应题面「必须验收」
├── src/
│   ├── cli.py                 # 命令行入口（ask / eval / stats）
│   ├── agent.py               # ReAct 主循环
│   ├── budget.py              # 步数上限 / 停滞检测 / token 预算
│   ├── tools.py               # 4 个工具 + 故障注入
│   ├── index.py               # 分词 + BM25 + 版本加权
│   ├── conflict.py            # 跨文档冲突检测（可判定 / 不可判定）
│   ├── verify.py              # 引用逐字校验 + 冲突兜底降级
│   ├── trace.py               # Trace 落盘（JSON + Markdown）
│   ├── prompt.py              # 系统提示词
│   ├── llm.py                 # 真实模型 / 离线模拟器
│   └── models.py              # 数据结构
├── tests/                     # 56 个测试：检索、预算、冲突、校验、端到端（含动态替换文档内容）、密钥自检、三轮评审回归
├── examples/                  # 真实模型（deepseek-flash）跑出的 7 组 trace
│   ├── 01-normal-answer/ … 07-tool-failure/
│   ├── summary.json           # 7/7 通过
│   └── offline-mock/          # 同一套用例的离线模拟器结果，用于对照
└── docs/
    ├── 01-requirements.md
    ├── 02-solution.md
    ├── 03-evaluation.md
    ├── 04-ai-usage.md
    └── 05-retrospective.md
```

## 3. 它是怎么工作的

真实模型下的两条典型决策路径（见 `examples/`）：

**路径 A · 跨文档冲突查证（`examples/02-doc-conflict/trace.md`，3 轮 7 次调用）**

| 轮 | 动作 | 观察 | 下一步决策 |
| --- | --- | --- | --- |
| 1 | 并发 `search_docs("报销期限")` + `search_docs("费用报销申请时效 规定时间内提交")` | 同时召回 D02#2（15 日）与 D01#2（30 日）摘要 | 发现两份文件规定不一致，进入原文核查与冲突判定 |
| 2 | 并发 `read_chunk("D01#2")`、`read_chunk("D02#2")`、`read_chunk("D02#1")` + `check_conflicts("报销期限")` | 读到两份期限原文 + D02#1「两者并行执行」，冲突判定为 `resolvable=false` | 均有效且无替代关系，不能二选一 |
| 3 | `submit_answer(need_confirmation)` | 引用 3 条全部通过逐字校验 | 并列列出 30 日 / 15 日与出处，请用户确认 |

> 对照组的串行版本见 `examples/offline-mock/02-doc-conflict/trace.md`（5 步：`search_docs` → `read_chunk(D02#2)` → `check_conflicts` → 补读 `read_chunk(D01#2)` → `submit_answer`）。

**路径 B · 检索弱命中后改写重试（`examples/03-out-of-scope/trace.md` & `examples/07-tool-failure/trace.md`）**

- **Q3（资料外问题）**：第 1 轮检索 `年会预算标准` / `会议活动费用预算审批` 返回 `weak/none` → 模型根据观察在第 2 轮改写为 `员工活动经费 团建费用标准 每人` / `年度庆典 大型活动 经费额度 专项预算` 重检 → 仍为 `weak` 后在第 3 轮提交 `insufficient`。
- **Q7（工具故障）**：第 1 轮检索定位到候选片段 → 第 2 轮 `read_chunk` 超时失败 → 第 3 轮模型换片段重试一次仍超时 → 第 4 轮按规则提交 `error`，绝不退回用 40 字检索摘要作答。

## 4. 六个必须验收点如何被覆盖

| 验收点 | 用例 | Trace |
| --- | --- | --- |
| 明确答案 | Q1 住宿标准 | `examples/01-normal-answer/` |
| 文档冲突 | Q2 报销期限 30 vs 15 | `examples/02-doc-conflict/` |
| 资料外问题 | Q3 年会预算 | `examples/03-out-of-scope/` |
| 改写后的相似问题 | Q4「住酒店一晚上限多少钱」 | `examples/04-query-rewrite/` |
| 版本冲突（额外覆盖） | Q5 年假 7 vs 5 | `examples/05-version-conflict/` |
| 上下文追问 | Q6「那需要提前几天申请」 | `examples/06-followup/` |
| 工具失败 | Q7 read_chunk 全部超时 | `examples/07-tool-failure/` |

## 5. 三条硬约束（对应题面关键缺陷）

1. **引用必须来自已读原文且逐字一致** —— `verify.build_citations` 先核验该 `chunk_id` 是否已在本轮前经 `read_chunk` 成功读取（阻断直接抄 `search_docs` 40 字摘要的绕过路径），再做归一化子串比对；`answered` 与 `need_confirmation` 若无有效引用，均强制降级为 `insufficient`。
2. **不得凭常识补答** —— 检索 `hit_quality=none/weak` 时先改写查询；无可改写方向或改写后仍无命中，返回 `insufficient` 并说明缺什么。
3. **冲突不得二选一** —— 冲突分两类：有替代关系/生效日期/唯一具体适用范围可判定的，按胜出文件作答并附依据；无法判定的，返回 `need_confirmation` 并并列列出取值。

另有系统侧兜底：即使模型漏调 `check_conflicts`，只要被引用片段命中未消解冲突（或所引文档在当前问题主题上存在未消解冲突），`apply_guardrails` 会强制把 `answered` 降级为 `need_confirmation`。

## 6. 实测结果

`examples/` 下是**真实模型**（`deepseek-flash` / DeepSeek-V4.1-Flash）跑出的 trace，
`examples/offline-mock/` 是同一套用例在离线模拟器下的结果，用于对照。

| 模式 | 结果 | 说明 |
| --- | --- | --- |
| `live:deepseek-flash` | **7/7 通过** | 主证据，见 `examples/summary.json` |
| `offline:scripted` | 7/7 通过 | 对照，只证明工程链路 |
| `pytest` | 56 passed | 含 3 条密钥泄漏自检 + 21 条三轮独立评审回归 + 动态替换文档内容验证 |

真实模型的表现与离线模拟器有两处明显差异，值得注意：

- **检索更主动**：会并发多个措辞同时检索（Q1 首轮发 2 条 query），命中率更高。
- **举证更完整**：Q5 会额外读取 D04#2（版本说明）作为"旧版已废止"的引用，而不是只引新版正文。

> 交付前做过**三轮独立模型代码审阅与候选人逐行复核**，累计修复了无工具直答绕过校验、未读原文凭摘要引用、同轮并发未观察提前提交、冲突主题误伤/漏拦、新旧数值混搭放行等隐患，并补齐 21 条回归测试，详见 `docs/03-evaluation.md` 第 7 节。

## 7. 已知限制

- **离线模拟器不等于模型能力。** `--offline` 用规则复现决策链，只验证工程链路、Trace 结构与校验层是否工作。真实效果以上表 live 结果为准。
- **事实是预抽取的。** `data/docs.json` 的 `facts` 字段在构建期人工标注。真实场景应由抽取模型 + 人工校验生成，本项目的范围只到「冲突判定」这一层。
- **检索是词面匹配。** 用 BM25 替代向量检索，是为了零依赖、可复现。跨语言、同义改写能力有限，所以把改写责任显式交给模型（Q4 即验证此路径）。
- **冲突相关性判定是启发式的。** `conflicts_for_query` 用「键名前两字出现在查询中」或「≥2 个 token 重叠」筛相关冲突，措辞差异大时可能漏检或误带无关冲突。
- **单轮问答为主。** 上下文追问只做了「指代 → 合并上一轮问句」这一层，未做完整的多轮状态管理。
- **无权限体系。** 题面未要求，未实现文档级权限过滤。

## 8. 密钥与安全

本项目**不含任何真实密钥**，密钥只通过环境变量注入：

```bash
export KB_AGENT_API_KEY=...        # 仅当前 shell 有效，不落盘
```

三重防护：

1. `.gitignore` 忽略 `.env`、`.env.*`（保留 `.env.example`）、`*.key`、`*.pem`、`secrets.json`。
2. `.env.example` 中的 key 字段留空，只作占位。
3. `tests/test_no_secrets.py` 在 `pytest` 时扫描全仓库，命中 `sk-…` / `AKIA…` / 私钥头即失败。

提交前建议再手工确认一次：

```bash
git status --short          # 确认没有 .env 出现在待提交列表
git grep -n "sk-" -- . ':!README.md'   # 应为空
```

> 如果 key 曾经出现在聊天记录、截图或公开仓库里，请到控制台**吊销并重新生成**，
> 环境变量方案只保证"不写进代码"，不保证"没有外泄过"。

## 9. 命令速查

```bash
python -m src.cli stats                                  # 知识库规模
python -m src.cli ask "年假有几天？" --offline -v         # 单次问答（含每步）
python -m src.cli ask "那需要提前几天申请？" \
       --context "年假有几天？" --offline                  # 带上下文
python -m src.cli ask "报销期限是多久？" --fail-tool read_chunk   # 注入工具故障
python -m src.cli eval --offline --summary examples/summary.json  # 离线回归
python -m src.cli eval --summary examples/summary.json            # 真实模型回归（需 key）
```

## 10. 用时与未完成部分

题面要求"记录实际用时和未完成部分"，这里如实列出。

### 10.1 实际用时

| 阶段 | 用时 | 说明 |
| --- | --- | --- |
| 读题 + 方案设计 | 约 10 分钟 | 四道题比选、决定不合并既有两个项目 |
| 生成骨架与代码 | 约 20 分钟 | AI 辅助编码，含 12 个源文件 / 6 个测试文件 |
| 实测与修复 | 约 15 分钟 | 3 个 bug + 1 处评估标准修正（见 `docs/03-evaluation.md` 第 4 节） |
| 文档 | 约 10 分钟 | docs/01–05 初稿 |
| **小计** | **约 55 分钟** | 远低于建议的 3 小时，因为编码由 AI 承担 |
| 候选人本人复核与三轮审查修复 | **约 45 分钟** | 逐行通读 `src/` 核心模块、核对 7 组 Trace 引用、完成三轮审查闭环 |

> 用时低于建议值不等于质量达标。省下来的时间应该投到人工复核上——
> 这一步没做，等于把"AI 生成"直接当成"已验证"。

### 10.2 未完成部分

| 项 | 现状 | 影响 |
| --- | --- | --- |
| 语义级事实蕴含校验 | 仅做逐字子串 + 数值一致性检查 | 数值对得上但语义相反的极端错答需独立 NLI 模型 |
| 大规模语料验证 | 仅 10 篇 / 29 片段 | 数百篇下的召回质量未知 |
| 其他模型适配 | 只测 `deepseek-flash` | 换模型后提示词约束是否仍生效未知 |
| 向量 / 混合检索 | 只有 BM25 | 同义改写完全依赖模型，改写失败即拒答 |
| 多轮状态管理 | 仅一层指代合并 | 指代链、话题切换会失效 |
| 文档级权限 | 未实现 | 企业场景必需，题面未要求 |
| 成本与并发 | 未统计 | 7 组用例约 40 次模型调用，token 成本未核算 |
