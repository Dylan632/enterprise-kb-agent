# examples 说明

本目录是 7 组验收用例的**实际运行记录**，不是手写样例。

## 目录

```text
examples/
├── 01-normal-answer/        明确答案
├── 02-doc-conflict/         文档冲突（不可判定）
├── 03-out-of-scope/         资料外问题
├── 04-query-rewrite/        改写后的相似问题
├── 05-version-conflict/     版本冲突（可判定）
├── 06-followup/             上下文追问
├── 07-tool-failure/         工具失败
├── summary.json             live 模式 7/7 通过
└── offline-mock/            同一套用例的离线模拟器结果（对照组）
```

每组含两个文件：

- `trace.json` —— 机器可读，字段见 `src/models.py::Trace`
- `trace.md` —— 评审可读，含执行链表格、最终答案、引用核对

## 两种模式的区别

| | 顶层目录（live） | `offline-mock/` |
| --- | --- | --- |
| 模型 | `deepseek-flash`（DeepSeek-V4.1-Flash） | 规则模拟器 |
| 能证明什么 | 模型在提示词约束下的真实决策行为 | 仅工程链路、Trace 结构、校验层 |
| 能否作为效果依据 | **能** | 不能 |

题面要求 Mock 必须标明边界，这里把两组结果并列存放，便于直接对照。

## 怎么看一条 trace

以 `02-doc-conflict/trace.md` 为例，执行链表格的五列对应题面要求的五项：

| 列 | 题面要求 |
| --- | --- |
| 步 | 步骤 |
| 工具 / 参数 | 工具参数 |
| 返回摘要 | 返回摘要 |
| 下一步 | 后续动作 |
| 决策依据 | 简要决策依据 |

第 3 步"查冲突"之后，第 4 步才去读 D01#2——这个顺序由工具返回结果决定，
不是预设流程。对照组 `01-normal-answer` 没有冲突，因此少了两步。
两条路径的差异就是"模型调整下一步"的证据。

## 复现

```bash
python -m src.cli eval --offline --out examples/offline-mock   # 对照组
python -m src.cli eval                                          # live，需 KB_AGENT_API_KEY
```
