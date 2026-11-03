"""Agent 主循环。

循环形态严格按题面要求：
    模型选择动作 → 执行工具 → 观察结果 → 模型调整下一步或结束

退出权在模型手里：模型调用 submit_answer 即结束。
Budget 只在三种异常情况下兜底（步数上限 / 死循环 / token 预算），
并返回明确的 unfinished 状态——这正是题面要求的「达到上限时返回未完成状态」。
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from src.budget import Budget, ExitReason
from src.conflict import conflicts_for_query
from src.index import KnowledgeIndex
from src.llm import LLMClient, LLMError
from src.models import FinalAnswer, StepRecord, Trace, VALID_STATUSES
from src.prompt import SYSTEM_PROMPT
from src.tools import FailureInjector, ToolRegistry
from src.verify import apply_guardrails, build_citations


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Agent:
    def __init__(
        self,
        index: KnowledgeIndex,
        client: LLMClient,
        max_steps: int = 8,
        injector: Optional[FailureInjector] = None,
    ):
        self.index = index
        self.client = client
        self.max_steps = max_steps
        self.injector = injector or FailureInjector()

    # ------------------------------------------------------------------ #

    def run(self, question: str, history: Optional[Sequence[Dict[str, str]]] = None) -> Trace:
        registry = ToolRegistry(self.index, self.injector)
        budget = Budget(max_steps=self.max_steps, stagnation_window=3)

        trace = Trace(
            run_id=f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}",
            question=question,
            mode=self.client.name,
            model=self.client.name,
            history=list(history or []),
            started_at=_now_iso(),
        )

        messages: List[Dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        for turn in history or []:
            messages.append({"role": "user", "content": turn.get("question", "")})
            messages.append({"role": "assistant", "content": turn.get("answer", "")})
        messages.append({"role": "user", "content": question})

        started = time.perf_counter()
        final: Optional[FinalAnswer] = None
        conflict_pool = []

        while True:
            reason = budget.check()
            if reason != ExitReason.WITHIN_BUDGET:
                trace.exit_reason = reason
                final = FinalAnswer(
                    status="unfinished",
                    answer=f"{ExitReason.describe(reason)}（已执行 {budget.step} 步）。请缩小问题范围或稍后重试。",
                    warnings=[reason],
                )
                break

            step_no = budget.begin_step()

            try:
                response = self.client.chat(messages, registry.definitions())
            except LLMError as exc:
                trace.exit_reason = "llm_error"
                final = FinalAnswer(status="error", answer=f"模型调用失败：{exc}", warnings=["llm_error"])
                break

            budget.record_tokens(response.input_tokens, response.output_tokens)

            # 模型没调工具就想结束：不能直接采信。
            # 这条路径没有经过 submit_answer，天然没有结构化引用，
            # 若直接标成 answered 就等于绕过了全部校验（外部评审 P0 指出）。
            # 统一送进 apply_guardrails：无有效引用会被降级为 insufficient。
            if not response.tool_calls:
                final = apply_guardrails(
                    self.index,
                    FinalAnswer(status="answered", answer=(response.content or "").strip()),
                    question,
                )
                final.warnings.append("模型未调用任何工具、未提交结构化答案，已按校验结果处理")
                trace.steps.append(
                    StepRecord(
                        step=step_no,
                        thought=(response.content or "").strip()[:300],
                        tool="(none)",
                        arguments={},
                        result_summary=f"模型未调用工具，直接返回文本；校验后状态 {final.status}",
                        next_action="结束",
                        reason="模型判定无需继续调用工具，但缺少可核验引用",
                        ok=final.status == "answered",
                    )
                )
                break

            budget.record_tool_calls([(tc.name, tc.arguments) for tc in response.tool_calls])
            messages.append(
                {
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)},
                        }
                        for tc in response.tool_calls
                    ],
                }
            )

            # 记录进入本轮前已成功读取过的片段：submit_answer 只能引用此前已观察到原文的片段
            prior_read_chunks = set(registry.read_chunks)
            has_other_tools = any(tc.name != "submit_answer" for tc in response.tool_calls)

            for tc in response.tool_calls:
                if tc.name == "submit_answer" and has_other_tools:
                    # 同轮并发了其他取数/检查工具时，模型尚未观察到本轮工具返回结果，不得提前结算退出
                    result = registry.reject_premature_submit()
                else:
                    result = registry.execute(tc.name, tc.arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(
                            {
                                "ok": result.ok,
                                "error": result.error,
                                **result.payload,
                            },
                            ensure_ascii=False,
                        ),
                    }
                )
                if tc.name == "check_conflicts" and result.ok:
                    conflict_pool = result.payload.get("conflicts", [])

                trace.steps.append(
                    StepRecord(
                        step=step_no,
                        thought=(response.content or "").strip()[:300],
                        tool=tc.name,
                        arguments=tc.arguments,
                        result_summary=result.summary if result.ok else f"失败：{result.error}",
                        next_action="",  # 后处理填充
                        reason="",
                        ok=result.ok,
                        latency_ms=result.latency_ms,
                    )
                )

            if registry.submitted is not None:
                final = self._finalize(registry.submitted, question, conflict_pool, prior_read_chunks)
                break

        trace.duration_ms = int((time.perf_counter() - started) * 1000)
        trace.final = final if final is not None else FinalAnswer(status="error", answer="未产生结果")
        self._link_steps(trace.steps)
        return trace

    # ------------------------------------------------------------------ #

    def _finalize(
        self,
        submitted: Dict[str, Any],
        question: str,
        conflict_pool: List[Dict[str, Any]],
        read_chunks: Optional[set] = None,
    ) -> FinalAnswer:
        raw_status = str(submitted.get("status", "")).strip()
        # 未知状态必须失败关闭。若回退到 answered，一个拼错的 status 加上一条真实引用
        # 就能把"未经支持的答案"标成已回答（外部评审 P1 指出）。
        if raw_status not in VALID_STATUSES:
            return FinalAnswer(
                status="error",
                answer=f"模型返回了无法识别的状态「{raw_status or '空'}」，已按失败处理，不采信其内容。",
                warnings=[f"unknown_status:{raw_status or 'empty'}"],
            )
        status = raw_status
        citations = build_citations(self.index, submitted.get("citations") or [], read_chunks=read_chunks)
        answer = FinalAnswer(
            status=status,
            answer=str(submitted.get("answer", "")).strip(),
            citations=citations,
            missing=str(submitted.get("missing", "")).strip(),
        )
        # 模型没查冲突时，用系统检测结果补齐，供 guardrail 使用
        if not conflict_pool:
            conflict_pool = [g.to_dict() for g in conflicts_for_query(self.index, question)]
        return apply_guardrails(
            self.index,
            answer,
            question,
            read_chunks=read_chunks,
            conflict_pool=conflict_pool,
        )

    @staticmethod
    def _link_steps(steps: List[StepRecord]) -> None:
        """后处理：把「下一步动作」与「决策依据」回填到上一步，形成完整的决策链。

        一轮模型回复可能并发多个工具调用，它们共享同一个 step 编号。
        因此按「轮次」而不是按「调用」来连线：同一轮的所有调用，都指向下一轮的动作集合。
        """
        submit_reasons = {
            "answered": "已核实原文且无未消解冲突，提交最终答案",
            "need_confirmation": "发现跨文档未消解冲突，提交待用户确认",
            "insufficient": "检索无相关制度依据，按资料不足停止作答",
            "error": "原文读取连续失败，无法核实条款原文，停止作答",
        }
        for idx, step in enumerate(steps):
            nxt = next((s for s in steps[idx + 1 :] if s.step > step.step), None)
            if nxt is not None:
                nxt_turn = [s for s in steps[idx + 1 :] if s.step == nxt.step]
                unique_tools = list(dict.fromkeys(s.tool for s in nxt_turn))
                step.next_action = " + ".join(unique_tools)
                if nxt.thought:
                    step.reason = nxt.thought
                elif nxt.tool == "submit_answer":
                    st = str(nxt.arguments.get("status", ""))
                    step.reason = submit_reasons.get(st, "根据上一轮观察提交最终结果")
                else:
                    step.reason = "根据上一轮观察继续"
            else:
                step.next_action = "结束"
                step.reason = "模型提交最终答案" if step.tool == "submit_answer" else "任务结束"
