"""预算测试：三道保险阀必须各自可触发，且能给出明确原因。"""
from __future__ import annotations

import pytest

from src.budget import Budget, ExitReason


def test_within_budget_before_limits():
    budget = Budget(max_steps=3)
    budget.begin_step()
    assert budget.check() == ExitReason.WITHIN_BUDGET


def test_hard_step_limit_triggers():
    budget = Budget(max_steps=2)
    budget.begin_step()
    budget.begin_step()
    assert budget.check() == ExitReason.HARD_STEP_LIMIT
    assert "步数上限" in ExitReason.describe(ExitReason.HARD_STEP_LIMIT)


def test_stagnation_detected_on_repeated_identical_calls():
    budget = Budget(max_steps=10, stagnation_window=3)
    for _ in range(3):
        budget.begin_step()
        budget.record_tool_calls([("search_docs", {"query": "同一个查询"})])
    assert budget.check() == ExitReason.STAGNATION


def test_different_calls_do_not_trigger_stagnation():
    budget = Budget(max_steps=10, stagnation_window=3)
    for query in ("查询一", "查询二", "查询三"):
        budget.begin_step()
        budget.record_tool_calls([("search_docs", {"query": query})])
    assert budget.check() == ExitReason.WITHIN_BUDGET


def test_token_budget_triggers():
    budget = Budget(max_steps=10, token_budget=100)
    budget.record_tokens(60, 50)
    assert budget.check() == ExitReason.TOKEN_BUDGET
    assert budget.total_tokens == 110


def test_invalid_configuration_rejected():
    with pytest.raises(ValueError):
        Budget(max_steps=0)
    with pytest.raises(ValueError):
        Budget(stagnation_window=1)
