"""外部评审（另一位模型做静态审查）发现的问题的回归测试。

保留这个文件是为了让每条修复都有对应断言，避免以后改动把它悄悄改回去。
每条用例的 docstring 注明评审指出的问题，便于追溯。
"""
from __future__ import annotations

import pytest

from src.agent import Agent
from src.conflict import detect_conflicts
from src.index import KnowledgeIndex
from src.llm import ChatResponse, LLMClient
from src.models import Chunk, Citation, Doc, Fact, FinalAnswer, ToolCall
from src.verify import apply_guardrails


class StubClient(LLMClient):
    """按预设序列返回响应，用于构造特定的模型行为。"""

    name = "stub"

    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0

    def chat(self, messages, tools):
        resp = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return resp


# --------------------------------------------------------------------------- #
# P0-1：模型不调工具直接作答，曾绕过全部校验
# --------------------------------------------------------------------------- #


def test_text_answer_without_any_tool_call_is_not_accepted(index: KnowledgeIndex):
    """评审原话：agent.py 在模型未调用工具时直接设为 answered，绕过了 apply_guardrails。"""
    agent = Agent(index, StubClient([ChatResponse(content="报销期限是 30 天。")]))
    trace = agent.run("报销期限是多久？")

    assert trace.final.status != "answered", "无工具、无引用的文本不得被当作已回答"
    assert trace.final.status == "insufficient"
    assert any("未调用任何工具" in w or "缺少有效引用" in w for w in trace.final.warnings)
    assert trace.steps[-1].tool == "(none)"


def test_text_answer_does_not_bypass_guardrail_even_if_it_looks_complete(index: KnowledgeIndex):
    """即使文本里自称引用了 D01，也不能凭这句话通过校验。"""
    agent = Agent(index, StubClient([ChatResponse(content="根据 D01，报销期限是 30 日。")]))
    trace = agent.run("报销期限是多久？")
    assert trace.final.status == "insufficient"
    assert trace.final.citations == []


# --------------------------------------------------------------------------- #
# P1-1：未知 status 曾被回退成 answered
# --------------------------------------------------------------------------- #


def test_unknown_status_fails_closed(index: KnowledgeIndex):
    """评审原话：拼错的 status 加上一条真实引用，仍可能被标为已回答。"""
    bad = ChatResponse(
        content="提交",
        tool_calls=[
            ToolCall(
                id="c1",
                name="submit_answer",
                arguments={
                    "status": "answerd",  # 故意拼错
                    "answer": "报销期限是 30 日。",
                    "citations": [
                        {
                            "doc_id": "D01",
                            "chunk_id": "D01#2",
                            "quote": "员工应在差旅费用发生之日起 30 日内提交报销申请。",
                        }
                    ],
                },
            )
        ],
    )
    trace = Agent(index, StubClient([bad])).run("报销期限是多久？")
    assert trace.final.status == "error"
    assert any("unknown_status" in w for w in trace.final.warnings)


# --------------------------------------------------------------------------- #
# P1-2：引用真实性校验曾被当成事实正确性保证
# --------------------------------------------------------------------------- #


def test_answer_quantity_missing_from_citation_is_downgraded(index: KnowledgeIndex):
    """答案写 30 日，引用却是 15 日那一句 —— 引用真实，但支撑不了答案。"""
    answer = FinalAnswer(
        status="answered",
        answer="报销期限是 30 日。",
        citations=[Citation("D02", "D02#2", "所有费用报销须在费用发生之日起 15 日内提交，逾期系统自动关闭单据，需走线下特殊审批。", True)],
    )
    result = apply_guardrails(index, answer, "报销期限是多久？")
    assert result.status == "insufficient"
    assert any("未出现在引用原文" in w for w in result.warnings)


def test_matching_quantity_passes_numeric_check(index: KnowledgeIndex):
    answer = FinalAnswer(
        status="answered",
        answer="一线城市住宿标准为 500 元/晚。",
        citations=[Citation("D01", "D01#3", "一线城市（北京、上海、广州、深圳）住宿标准为 500 元/晚，其他城市为 350 元/晚。", True)],
    )
    result = apply_guardrails(index, answer, "一线城市住宿标准是多少？")
    assert result.status == "answered"


# --------------------------------------------------------------------------- #
# P2-1：冲突分组曾忽略适用范围
# --------------------------------------------------------------------------- #


def _make_doc(doc_id: str, scope: str, value: str, key: str = "报销期限") -> Doc:
    return Doc(
        doc_id=doc_id,
        title=f"{doc_id} 测试文件",
        owner="测试部",
        version="v1.0",
        effective_date="2024-01-01",
        scope=scope,
        chunks=[Chunk(doc_id=doc_id, chunk_id=f"{doc_id}#1", heading=key, text=f"{key}为 {value}。")],
        facts=[Fact(key=key, value=value, chunk_id=f"{doc_id}#1")],
    )


def test_disjoint_scopes_are_not_a_conflict():
    """华东区与华南区各管各的，同名事实取值不同也不构成冲突。"""
    index = KnowledgeIndex([_make_doc("X1", "华东区", "30 日"), _make_doc("X2", "华南区", "15 日")])
    assert detect_conflicts(index) == []


def test_specific_scope_overrides_company_wide():
    """全公司与具体范围相交时，具体范围在其范围内优先，属可判定冲突。"""
    index = KnowledgeIndex([_make_doc("X1", "全公司", "30 日"), _make_doc("X2", "华东区", "15 日")])
    groups = detect_conflicts(index)
    assert len(groups) == 1
    assert groups[0].resolvable is True
    assert groups[0].winner_doc_id == "X2"
    assert "华东区" in groups[0].reason


def test_same_scope_different_values_still_unresolvable(index: KnowledgeIndex):
    """回归保护：真正该判冲突的场景不能因为 scope 改动而漏掉。"""
    groups = [g for g in detect_conflicts(index) if g.key == "报销期限"]
    assert groups and groups[0].resolvable is False


# --------------------------------------------------------------------------- #
# P2-2：eval 曾只看状态与引用文档
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,needle,expected",
    [
        ("住宿标准为 500 元/晚", "500元", True),
        ("住宿标准为 500 元/晚", "500 元", True),
        ("住宿标准为 350 元/晚", "500", False),
        ("年假 7 天", "7天", True),
    ],
)
def test_answer_contains_ignores_whitespace(text, needle, expected):
    from src.cli import _contains

    assert _contains(text, needle) is expected
