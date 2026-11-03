"""端到端测试：用离线模拟器跑完整决策链。

注意：这里断言的是「工程链路与校验层是否正确」，不是「模型决策质量」。
真实模型的效果必须用 live 模式跑 data/eval_questions.json，两者不可互相替代。
"""
from __future__ import annotations

from src.agent import Agent
from src.index import KnowledgeIndex
from src.llm import OfflineScriptedClient
from src.tools import FailureInjector


def _run(index: KnowledgeIndex, question: str, fail=(), history=None, max_steps=8):
    agent = Agent(
        index,
        OfflineScriptedClient(),
        max_steps=max_steps,
        injector=FailureInjector(fail_always=set(fail)),
    )
    return agent.run(question, history)


def test_normal_answer_has_traceable_citation(index: KnowledgeIndex):
    trace = _run(index, "一线城市出差住宿标准是多少？")
    assert trace.final.status == "answered"
    assert trace.final.citations and all(c.valid for c in trace.final.citations)
    assert "500" in trace.final.answer
    assert [s.tool for s in trace.steps][0] == "search_docs"
    assert trace.steps[-1].tool == "submit_answer"


def test_trace_steps_have_full_decision_fields(index: KnowledgeIndex):
    """题面要求每步都能看到 参数 / 返回摘要 / 后续动作 / 决策依据。"""
    trace = _run(index, "一线城市出差住宿标准是多少？")
    for step in trace.steps[:-1]:
        assert step.arguments, f"第 {step.step} 步缺少工具参数"
        assert step.result_summary
        assert step.next_action
        assert step.reason


def test_conflict_requires_confirmation(index: KnowledgeIndex):
    trace = _run(index, "报销期限是多久？")
    assert trace.final.status == "need_confirmation"
    assert {c.doc_id for c in trace.final.citations} == {"D01", "D02"}
    assert "30" in trace.final.answer and "15" in trace.final.answer


def test_version_conflict_resolved_with_reason(index: KnowledgeIndex):
    trace = _run(index, "年假有几天？")
    assert trace.final.status == "answered"
    assert {c.doc_id for c in trace.final.citations} == {"D03"}
    assert "依据" in trace.final.answer


def test_out_of_scope_does_not_hallucinate(index: KnowledgeIndex):
    trace = _run(index, "公司年会的预算是多少？")
    assert trace.final.status == "insufficient"
    assert trace.final.citations == []


def test_query_rewrite_path_is_visible_in_trace(index: KnowledgeIndex):
    trace = _run(index, "住酒店一晚上限多少钱？")
    assert trace.final.status == "answered"
    queries = [s.arguments.get("query") for s in trace.steps if s.tool == "search_docs"]
    assert len(queries) >= 2, "应当出现改写后的二次检索"
    assert queries[0] != queries[1]


def test_followup_uses_history(index: KnowledgeIndex):
    first = _run(index, "年假有几天？")
    history = [{"question": "年假有几天？", "answer": first.final.answer}]
    trace = _run(index, "那需要提前几天申请？", history=history)
    assert trace.final.status == "answered"
    assert "3 个工作日" in trace.final.answer


def test_tool_failure_stops_instead_of_guessing(index: KnowledgeIndex):
    trace = _run(index, "报销期限是多久？", fail=("read_chunk",))
    assert trace.final.status == "error"
    assert trace.final.citations == []
    assert any(not s.ok for s in trace.steps)


def test_step_limit_returns_unfinished(index: KnowledgeIndex):
    trace = _run(index, "报销期限是多久？", max_steps=2)
    assert trace.final.status == "unfinished"
    assert trace.exit_reason == "hard_step_limit"


def test_replacing_doc_content_dynamically_changes_answer_and_actions(index: KnowledgeIndex):
    """对应题面「审核要点：替换文档内容，核对答案与动作是否改变」。

    场景 A：把 D02 标为明确替代 D01（supersedes="D01"），再次询问「报销期限是多久？」：
            冲突变为可判定，Agent 不再需要补读 D01 并列求确认，直接返回 answered（15 日）。
    场景 B：把 D01#3 的住宿标准从 500 元/晚替换为 800 元/晚，再次询问「一线城市出差住宿标准是多少？」：
            Agent 返回的答案与逐字引用同步变为 800 元/晚。
    """
    import copy

    # 场景 A：修改 D02 与 D01 的替代关系
    mutated_a = copy.deepcopy(index)
    mutated_a.docs["D02"].supersedes = "D01"
    mutated_a.docs["D01"].superseded_by = "D02"
    mutated_a.docs["D01"].status = "archived"

    trace_a = _run(mutated_a, "报销期限是多久？")
    assert trace_a.final.status == "answered"
    assert "15 日" in trace_a.final.answer
    assert {c.doc_id for c in trace_a.final.citations} == {"D02"}

    # 场景 B：替换 D01#3 条款原文内容
    mutated_b = copy.deepcopy(index)
    doc_d01, chunk_d01_3 = mutated_b.chunks["D01#3"]
    chunk_d01_3.text = "一线城市（北京、上海、广州、深圳）住宿标准为 800 元/晚，其他城市为 450 元/晚。超标部分由个人承担。"

    trace_b = _run(mutated_b, "一线城市出差住宿标准是多少？")
    assert trace_b.final.status == "answered"
    assert "800 元/晚" in trace_b.final.answer
    assert "500" not in trace_b.final.answer
    assert trace_b.final.citations[0].quote.startswith("一线城市（北京、上海、广州、深圳）住宿标准为 800 元/晚")

