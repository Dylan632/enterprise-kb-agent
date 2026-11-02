"""跨文档冲突检测。

题面原话：「资料冲突时说明冲突并查证，不凭空选一个答案」。
这里把冲突拆成两类，对应两种不同的正确行为：

1. 可判定冲突（resolvable）：文档之间存在 supersedes / 生效日期先后关系。
   → 采用新版，但必须在答案里说明依据（哪份文件替代了哪份）。
2. 不可判定冲突（unresolvable）：两份文件都有效、作用域相同、且未声明优先级。
   → 不得选择，返回 need_confirmation，把冲突值与原文件一并列出。

检测范围是「文档预抽取的结构化事实」（data/docs.json 的 facts 字段）。
之所以预抽取而不是运行时用模型抽，是为了让冲突判定可测试、可复现——
冲突检测本身如果依赖模型，就无法写出稳定的单元测试。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Set

from src.index import KnowledgeIndex, tokenize
from src.models import ConflictEntry, ConflictGroup, Doc


def _group_facts(index: KnowledgeIndex) -> Dict[str, List[tuple]]:
    groups: Dict[str, List[tuple]] = {}
    for doc, fact in index.all_facts():
        groups.setdefault(fact.key, []).append((doc, fact))
    return groups


COMPANY_WIDE = "全公司"


def _scope_overlaps(a: str, b: str) -> bool:
    """两个适用范围是否有交集。全公司视为与任何范围相交。"""
    return a == b or a == COMPANY_WIDE or b == COMPANY_WIDE


def _scopes_disjoint(docs: List[Doc]) -> bool:
    """所有文档的适用范围两两不相交 → 各自生效，不构成冲突。"""
    for i in range(len(docs)):
        for j in range(i + 1, len(docs)):
            if _scope_overlaps(docs[i].scope, docs[j].scope):
                return False
    return True


def _resolve(entries: List[tuple]) -> tuple[bool, Optional[str], str]:
    """判断一组事实是否可判定，返回 (resolvable, winner_doc_id, reason)。"""
    docs: List[Doc] = [d for d, _ in entries]
    doc_ids = {d.doc_id for d in docs}

    for doc in docs:
        if doc.supersedes and doc.supersedes in doc_ids:
            if doc.status == "active":
                target = next(d for d in docs if d.doc_id == doc.supersedes)
                return (
                    True,
                    doc.doc_id,
                    f"{doc.doc_id}（{doc.version}，{doc.effective_date} 生效）明确替代 "
                    f"{target.doc_id}（{target.version}），以 {doc.doc_id} 为准",
                )
    for doc in docs:
        if doc.superseded_by and doc.superseded_by in doc_ids:
            newer = next(d for d in docs if d.doc_id == doc.superseded_by)
            if newer.status == "active":
                return (
                    True,
                    newer.doc_id,
                    f"{doc.doc_id} 已被 {newer.doc_id} 替代（见 {doc.doc_id} 版本说明），以 {newer.doc_id} 为准",
                )

    # 适用范围优先于"谁更新"：具体范围的规定在其范围内覆盖全公司规定。
    # 外部评审指出此前注释声称"作用域相同"，实现却没比较 scope，这里补上。
    specific = [d for d in docs if d.scope != COMPANY_WIDE]
    company = [d for d in docs if d.scope == COMPANY_WIDE]
    if specific and company and len({d.scope for d in specific}) == 1:
        winner = specific[0]
        return (
            True,
            winner.doc_id,
            f"{winner.doc_id} 仅适用于「{winner.scope}」，在该范围内优先于全公司规定 "
            f"{'、'.join(d.doc_id for d in company)}",
        )

    active = [d for d in docs if d.status == "active"]
    if len(active) < 2:
        return True, (active[0].doc_id if active else None), "仅一份文件处于有效状态"

    names = "、".join(f"{d.doc_id}（{d.version}，适用：{d.scope}）" for d in active)
    return (
        False,
        None,
        f"{names} 均处于有效状态、适用范围相交，且未声明优先级或替代关系，无法判定应适用哪一份",
    )


def detect_conflicts(index: KnowledgeIndex, doc_ids: Optional[Sequence[str]] = None) -> List[ConflictGroup]:
    """检测冲突。doc_ids 不为空时只返回涉及这些文档的冲突。"""
    wanted: Optional[Set[str]] = set(doc_ids) if doc_ids else None
    results: List[ConflictGroup] = []

    for key, entries in _group_facts(index).items():
        if len(entries) < 2:
            continue
        distinct = {fact.value for _, fact in entries}
        if len(distinct) < 2:
            continue
        if wanted is not None and not ({d.doc_id for d, _ in entries} & wanted):
            continue
        # 适用范围两两不相交 → 各自生效，不是冲突
        if _scopes_disjoint([d for d, _ in entries]):
            continue
        resolvable, winner, reason = _resolve(entries)
        results.append(
            ConflictGroup(
                key=key,
                entries=[
                    ConflictEntry(
                        doc_id=doc.doc_id,
                        doc_title=doc.title,
                        version=doc.version,
                        effective_date=doc.effective_date,
                        value=fact.value,
                        chunk_id=fact.chunk_id,
                        status=doc.status,
                    )
                    for doc, fact in entries
                ],
                resolvable=resolvable,
                winner_doc_id=winner,
                reason=reason,
            )
        )
    results.sort(key=lambda g: (g.resolvable, g.key))
    return results


def conflicts_for_query(index: KnowledgeIndex, query: str) -> List[ConflictGroup]:
    """按查询词筛出相关冲突。

    只用「任意 token 重叠」会把无关冲突带进来（例如「报销期限…天数」会误命中「年假天数」），
    干扰模型判断。这里改用两条更严的判据，任一满足即算相关：

    1. 冲突键的前两个字作为子串出现在查询里（中文场景下这一条命中率最高）
    2. 与冲突键有 ≥2 个 token 重叠
    """
    q_tokens = set(tokenize(query))
    if not q_tokens:
        return []
    picked: List[ConflictGroup] = []
    for group in detect_conflicts(index):
        head = group.key.split("（")[0]
        by_head = head[:2] in query if len(head) >= 2 else False
        by_tokens = len(set(tokenize(head)) & q_tokens) >= 2
        if by_head or by_tokens:
            picked.append(group)
    return picked


def unresolved_among(index: KnowledgeIndex, doc_ids: Sequence[str]) -> List[ConflictGroup]:
    """给定一批被引用的文档，返回它们之间仍未消解的冲突。用于最终答案的兜底校验。"""
    return [g for g in detect_conflicts(index, doc_ids) if not g.resolvable]
