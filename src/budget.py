"""执行预算：三道保险阀。

题面要求「项目须设置执行步数或工具调用次数上限，达到上限时返回明确的未完成状态」。
这里给出三个互不重叠的退出条件，先到先触发：

  HARD_STEP_LIMIT   硬步数上限（默认 8 步）
  STAGNATION        连续 N 步调用完全相同的工具+参数，判定为死循环
  TOKEN_BUDGET      累计 token 超过阈值（可选，默认关闭）

设计思路参考了本人在 PaiCLI 里的 AgentBudget：把「是否继续」的主权交给模型，
预算只做兜底，避免把正常的多次检索误判成异常。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple


class ExitReason:
    WITHIN_BUDGET = "within_budget"
    HARD_STEP_LIMIT = "hard_step_limit"
    STAGNATION = "stagnation"
    TOKEN_BUDGET = "token_budget"

    LABELS = {
        WITHIN_BUDGET: "未触发兜底条件",
        HARD_STEP_LIMIT: "达到步数上限，任务未完成",
        STAGNATION: "检测到连续重复的工具调用，判定为死循环",
        TOKEN_BUDGET: "token 预算已用尽",
    }

    @classmethod
    def describe(cls, reason: str) -> str:
        return cls.LABELS.get(reason, reason)


class Budget:
    def __init__(self, max_steps: int = 8, stagnation_window: int = 3, token_budget: Optional[int] = None):
        if max_steps <= 0:
            raise ValueError("max_steps 必须为正数")
        if stagnation_window < 2:
            raise ValueError("stagnation_window 必须 >= 2")
        self.max_steps = max_steps
        self.stagnation_window = stagnation_window
        self.token_budget = token_budget
        self.step = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self._signatures: List[str] = []
        self._stagnant = False

    def begin_step(self) -> int:
        self.step += 1
        return self.step

    def record_tokens(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += max(0, input_tokens)
        self.output_tokens += max(0, output_tokens)

    def record_tool_calls(self, calls: List[Tuple[str, Dict[str, Any]]]) -> None:
        if not calls:
            self._signatures.clear()
            return
        signature = "|".join(
            f"{name}:{json.dumps(args, ensure_ascii=False, sort_keys=True)}" for name, args in calls
        )
        self._signatures.append(signature)
        if len(self._signatures) > self.stagnation_window:
            self._signatures.pop(0)
        if len(self._signatures) == self.stagnation_window:
            self._stagnant = len(set(self._signatures)) == 1

    def check(self) -> str:
        if self._stagnant:
            return ExitReason.STAGNATION
        if self.token_budget and (self.input_tokens + self.output_tokens) >= self.token_budget:
            return ExitReason.TOKEN_BUDGET
        if self.step >= self.max_steps:
            return ExitReason.HARD_STEP_LIMIT
        return ExitReason.WITHIN_BUDGET

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens
