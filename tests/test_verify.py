"""引用校验与系统兜底测试。

这两个功能是「关键缺陷」的最后一道闸门，必须有独立测试，
不能只依赖 Agent 端到端跑通来间接验证。
"""
from __future__ import annotations

from src.index import KnowledgeIndex
from src.models import Citation, FinalAnswer
from src.verify import apply_guardrails, build_citations


def test_valid_quote_passes(index: KnowledgeIndex):
    citations = build_citations(
        index,
        [{"doc_id": "D01", "chunk_id": "D01#3", "quote": "一线城市（北京、上海、广州、深圳）住宿标准为 500 元/晚，其他城市为 350 元/晚。"}],
    )
    assert len(citations) == 1 and citations[0].valid


def test_rewritten_quote_is_rejected(index: KnowledgeIndex):
    citations = build_citations(
        index,
        [{"doc_id": "D01", "chunk_id": "D01#3", "quote": "一线城市住宿可以报销 500 元一晚。"}],
    )
    assert citations[0].valid is False
    assert "不一致" in citations[0].reason


def test_doc_id_mismatch_is_rejected(index: KnowledgeIndex):
    citations = build_citations(
        index,
        [{"doc_id": "D02", "chunk_id": "D01#3", "quote": "一线城市（北京、上海、广州、深圳）住宿标准为 500 元/晚"}],
    )
    assert citations[0].valid is False
    assert "doc_id" in citations[0].reason


def test_unknown_chunk_is_rejected(index: KnowledgeIndex):
    citations = build_citations(index, [{"doc_id": "D99", "chunk_id": "D99#1", "quote": "不存在的片段内容"}])
    assert citations[0].valid is False


def test_answered_without_citation_is_downgraded(index: KnowledgeIndex):
    answer = FinalAnswer(status="answered", answer="报销期限是 30 天。")
    result = apply_guardrails(index, answer, "报销期限是多久？")
    assert result.status == "insufficient"
    assert any("缺少有效引用" in w for w in result.warnings)


def test_unresolved_conflict_forces_confirmation(index: KnowledgeIndex):
    """模型若漏查冲突、只引用其中一份作答，系统必须拦下并降级。

    这是「双层防线」的意义：即便模型只看到 D01，也不能替用户拍板 30 日，
    因为 D02 的 15 日同样有效。
    """
    partial = FinalAnswer(
        status="answered",
        answer="报销期限是 30 日。",
        citations=[
            Citation("D01", "D01#2", "员工应在差旅费用发生之日起 30 日内提交报销申请。", True)
        ],
    )
    downgraded = apply_guardrails(index, partial, "报销期限是多久？")
    assert downgraded.status == "need_confirmation"
    assert downgraded.conflicts and downgraded.conflicts[0].key == "报销期限"

    # 换一个不涉及冲突的主题，不应误伤
    unrelated = FinalAnswer(
        status="answered",
        answer="一线城市住宿标准为 500 元/晚。",
        citations=[
            Citation("D01", "D01#3", "一线城市（北京、上海、广州、深圳）住宿标准为 500 元/晚", True)
        ],
    )
    kept = apply_guardrails(index, unrelated, "一线城市住宿标准是多少？")
    assert kept.status == "answered"
