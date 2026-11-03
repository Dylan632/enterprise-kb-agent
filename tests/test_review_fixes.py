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


# --------------------------------------------------------------------------- #
# 第二轮深度审阅回归测试（冲突误伤/漏拦、未读原文引用、部分数值编造、作用域边界）
# --------------------------------------------------------------------------- #


def test_reimbursement_hotel_question_does_not_false_trigger_deadline_conflict(index: KnowledgeIndex):
    """问「差旅住宿报销标准」含「报销」二字，但引用 D01#3（住宿标准）不应误触发「报销期限」冲突降级。"""
    answer = FinalAnswer(
        status="answered",
        answer="一线城市住宿标准为 500 元/晚，其他城市为 350 元/晚。",
        citations=[
            Citation("D01", "D01#3", "一线城市（北京、上海、广州、深圳）住宿标准为 500 元/晚，其他城市为 350 元/晚。", True)
        ],
    )
    res = apply_guardrails(index, answer, "差旅住宿报销标准是多少？")
    assert res.status == "answered"


def test_colloquial_deadline_question_citing_conflicting_chunk_is_caught(index: KnowledgeIndex):
    """口语问法不含「报销期限」词面，但只要引用了处于未消解冲突中的 D01#2，兜底仍必须拦下。"""
    answer = FinalAnswer(
        status="answered",
        answer="员工应在差旅费用发生之日起 30 日内提交申请。",
        citations=[
            Citation("D01", "D01#2", "员工应在差旅费用发生之日起 30 日内提交报销申请。", True)
        ],
    )
    res = apply_guardrails(index, answer, "出差回来最晚几天内交单据？")
    assert res.status == "need_confirmation"


def test_citing_unread_chunk_via_search_snippet_is_rejected(index: KnowledgeIndex):
    """未成功调用 read_chunk 时，模型直接抄 search_docs 的 40 字 snippet 充当引用必须被拒。"""
    from src.tools import FailureInjector

    responses = [
        ChatResponse(
            content="先检索",
            tool_calls=[ToolCall(id="1", name="search_docs", arguments={"query": "一线城市出差住宿标准"})],
        ),
        ChatResponse(
            content="不读原文直接用 snippet 提交",
            tool_calls=[
                ToolCall(
                    id="2",
                    name="submit_answer",
                    arguments={
                        "status": "answered",
                        "answer": "一线城市住宿标准为 500 元/晚，其他城市为 350 元/晚。",
                        "citations": [
                            {
                                "doc_id": "D01",
                                "chunk_id": "D01#3",
                                "quote": "一线城市（北京、上海、广州、深圳）住宿标准为 500 元/晚，其他城市为 350",
                            }
                        ],
                    },
                )
            ],
        ),
    ]
    trace = Agent(index, StubClient(responses), injector=FailureInjector(fail_always={"read_chunk"})).run(
        "一线城市出差住宿标准是多少？"
    )
    assert trace.final.status == "insufficient"
    assert trace.final.citations == []
    assert any("未调用 read_chunk" in w for w in trace.final.warnings)


def test_need_confirmation_without_valid_citation_is_downgraded(index: KnowledgeIndex):
    """need_confirmation 若无有效引用，同样不能放行，必须降级为 insufficient。"""
    answer = FinalAnswer(status="need_confirmation", answer="两份文件冲突：一份说 10 天，一份说 20 天。", citations=[])
    res = apply_guardrails(index, answer, "报销期限是多久？")
    assert res.status == "insufficient"


def test_partial_numeric_hallucination_is_downgraded(index: KnowledgeIndex):
    """答案中一个数字对上（350）、另一个关键数字编造（999），也必须降级为 insufficient。"""
    answer = FinalAnswer(
        status="answered",
        answer="一线城市住宿标准为 999 元/晚，其他城市为 350 元/晚。",
        citations=[
            Citation("D01", "D01#3", "一线城市（北京、上海、广州、深圳）住宿标准为 500 元/晚，其他城市为 350 元/晚。", True)
        ],
    )
    res = apply_guardrails(index, answer, "一线城市住宿标准是多少？")
    assert res.status == "insufficient"


def test_archived_specific_scope_does_not_override_active_company_wide():
    """已归档的区域制度不得覆盖现行有效的全公司制度。"""
    d_company = _make_doc("X1", "全公司", "30 日")
    d_archived_regional = _make_doc("X2", "华东区", "15 日")
    d_archived_regional.status = "archived"
    index = KnowledgeIndex([d_company, d_archived_regional])
    groups = detect_conflicts(index)
    assert len(groups) == 1
    assert groups[0].resolvable is True
    assert groups[0].winner_doc_id == "X1"


def test_conflicting_same_specific_scopes_stay_unresolvable():
    """同一区域内两份现行制度互相冲突时，即使另有全公司制度，也不得强行判其中一份胜出。"""
    index = KnowledgeIndex(
        [
            _make_doc("X1", "全公司", "30 日"),
            _make_doc("X2", "华东区", "15 日"),
            _make_doc("X3", "华东区", "20 日"),
        ]
    )
    groups = detect_conflicts(index)
    assert len(groups) == 1
    assert groups[0].resolvable is False
    assert groups[0].winner_doc_id is None


# --------------------------------------------------------------------------- #
# 第三轮深度审阅回归测试（同轮并发提前提交阻断、新旧数字混搭校验）
# --------------------------------------------------------------------------- #


def test_same_turn_read_chunk_and_submit_answer_is_blocked_until_next_turn(index: KnowledgeIndex):
    """同轮并发调用 read_chunk + submit_answer 时，模型尚未观察到 read_chunk 结果，不得提前退出。"""
    responses = [
        ChatResponse(
            content="企图同轮边读边交",
            tool_calls=[
                ToolCall(id="1", name="read_chunk", arguments={"chunk_id": "D01#3"}),
                ToolCall(
                    id="2",
                    name="submit_answer",
                    arguments={
                        "status": "answered",
                        "answer": "一线城市住宿标准为 500 元/晚，其他城市为 350 元/晚。",
                        "citations": [
                            {
                                "doc_id": "D01",
                                "chunk_id": "D01#3",
                                "quote": "一线城市（北京、上海、广州、深圳）住宿标准为 500 元/晚，其他城市为 350 元/晚。",
                            }
                        ],
                    },
                ),
            ],
        ),
        ChatResponse(
            content="已观察到 D01#3 原文，现在正式提交",
            tool_calls=[
                ToolCall(
                    id="3",
                    name="submit_answer",
                    arguments={
                        "status": "answered",
                        "answer": "一线城市住宿标准为 500 元/晚，其他城市为 350 元/晚。",
                        "citations": [
                            {
                                "doc_id": "D01",
                                "chunk_id": "D01#3",
                                "quote": "一线城市（北京、上海、广州、深圳）住宿标准为 500 元/晚，其他城市为 350 元/晚。",
                            }
                        ],
                    },
                )
            ],
        ),
    ]
    trace = Agent(index, StubClient(responses)).run("一线城市出差住宿标准是多少？")
    # 第一轮的 submit_answer 必须被拒（ok=False），进入第二轮观察后再成功提交
    assert trace.steps[1].tool == "submit_answer" and trace.steps[1].ok is False
    assert trace.steps[-1].step == 2 and trace.steps[-1].tool == "submit_answer" and trace.steps[-1].ok is True
    assert trace.final.status == "answered"


def test_archived_number_masquerading_as_current_rule_is_downgraded(index: KnowledgeIndex):
    """若答案未说明旧版背景，却把已读旧版 D04#1 的 5 天当成现行年假标准写入答案，必须被降级。"""
    bad_answer = FinalAnswer(
        status="answered",
        answer="按现行规定，入职满 1 年可享受带薪年假 5 天，需提前 3 个工作日申请。",
        citations=[
            Citation(
                "D03",
                "D03#1",
                "员工入职满 1 年可享受带薪年假 7 天，满 3 年 10 天，满 5 年 15 天。年假需提前 3 个工作日在 HR 系统中提交申请，经直属主管审批后生效。",
                True,
            )
        ],
    )
    res = apply_guardrails(index, bad_answer, "年假有几天？", read_chunks={"D03#1", "D04#1"})
    assert res.status == "insufficient"
    assert any("5" in w for w in res.warnings)


