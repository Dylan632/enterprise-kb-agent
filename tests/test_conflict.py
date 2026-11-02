"""冲突检测测试：两类冲突必须给出两种不同的结论。"""
from __future__ import annotations

from src.conflict import conflicts_for_query, detect_conflicts, unresolved_among
from src.index import KnowledgeIndex


def _group(index: KnowledgeIndex, key_prefix: str):
    for group in detect_conflicts(index):
        if group.key.startswith(key_prefix):
            return group
    raise AssertionError(f"未找到冲突键：{key_prefix}")


def test_parallel_rules_are_unresolvable(index: KnowledgeIndex):
    """D01 30 日 vs D02 15 日，D02 声明「并行执行」，无法判定。"""
    group = _group(index, "报销期限")
    assert group.resolvable is False
    assert group.winner_doc_id is None
    values = {e.doc_id: e.value for e in group.entries}
    assert values == {"D01": "30 日", "D02": "15 日"}


def test_supersede_chain_is_resolvable(index: KnowledgeIndex):
    """D03 明确替代 D04，可判定，胜出方必须是 D03。"""
    group = _group(index, "年假天数")
    assert group.resolvable is True
    assert group.winner_doc_id == "D03"
    assert "替代" in group.reason


def test_conflicts_for_query_filters_by_topic(index: KnowledgeIndex):
    assert [g.key for g in conflicts_for_query(index, "报销期限是多久？")] == ["报销期限"]
    assert conflicts_for_query(index, "会议室怎么预订？") == []


def test_unresolved_among_only_returns_unresolvable(index: KnowledgeIndex):
    groups = unresolved_among(index, ["D01", "D02"])
    assert [g.key for g in groups] == ["报销期限"]
    # D03/D04 的冲突可判定，不应出现在「未消解」列表里
    assert unresolved_among(index, ["D03", "D04"]) == []
