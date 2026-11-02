"""检索层测试：验证分词、排序、版本加权。

这些用例固定的是「检索行为」，不是「模型行为」——所以它们可以稳定断言。
"""
from __future__ import annotations

from src.index import KnowledgeIndex, classify, tokenize


def test_tokenize_keeps_cjk_bigrams_and_latin_words():
    tokens = tokenize("住宿标准 500 元/晚")
    assert "住宿" in tokens and "标准" in tokens
    assert "500" in tokens


def test_search_returns_expected_chunk_for_written_query(index: KnowledgeIndex):
    hits = index.search("一线城市出差住宿标准是多少？")
    assert hits, "应当有命中"
    assert hits[0].chunk_id == "D01#3"
    assert hits[0].hit_quality == "strong"


def test_colloquial_query_has_no_strong_hit(index: KnowledgeIndex):
    """口语化查询与原文无词面重叠，检索层应如实返回 none，而不是硬凑一个。"""
    hits = index.search("住酒店一晚上限多少钱？")
    assert all(h.hit_quality == "none" for h in hits)


def test_archived_doc_is_downweighted(index: KnowledgeIndex):
    """旧版文档更短，裸 BM25 会占优；版本加权后现行版本必须排在前面。"""
    hits = index.search("年假有几天？")
    assert hits[0].doc_id == "D03"
    assert any(h.doc_id == "D04" for h in hits), "旧版仍应可检索到，用于冲突举证"


def test_coverage_classification_boundaries():
    assert classify(0.5) == "strong"
    assert classify(0.2) == "weak"
    assert classify(0.05) == "none"


def test_read_chunk_returns_doc_meta(index: KnowledgeIndex):
    doc, chunk = index.get_chunk("D04#1")
    assert doc.status == "archived"
    assert doc.superseded_by == "D03"
    assert "年假" in chunk.text
