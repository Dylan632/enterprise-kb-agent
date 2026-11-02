"""答案校验层（系统侧兜底）。

题面把「无依据回答」和「错误引用」列为关键缺陷，而这两类问题都可能在模型侧漏检。
所以除了在提示词里要求模型自查，这里再做一层与模型无关的硬校验：

  1. 引用必须能定位到真实片段，且 quote 与原文逐字一致（归一化空白后比对）
  2. 答案中的关键数值必须能在引用原文里找到（弱事实一致性检查）
  3. status=answered 必须至少有一条有效引用，否则降级为 insufficient
  4. 若被引用的文档之间存在未消解冲突，且冲突主题与问题相关，强制降级为 need_confirmation

**边界声明（重要）**：
本模块做的是「引用真实性校验」——证明引文确实来自该片段，且答案里的数值没有脱离引文。
它**不保证**答案在语义上由引文推出。例如引文写「住宿标准 500 元」，答案却写
「超标可全额报销」，两者数值都对得上，但后者不是引文支持的结论。
要覆盖这类问题需要事实级断言或独立核验模型，不在本次范围内。
第 2 条只是把「数值完全对不上」这种最粗糙的错答拦下来，不等于语义校验。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence

from src.conflict import unresolved_among
from src.index import KnowledgeIndex, tokenize
from src.models import Citation, ConflictGroup, FinalAnswer

MIN_QUOTE_LEN = 8

# 只抽「数字 + 计量单位」的组合，避开年份、版本号、条款编号等噪声。
# 单位按长度优先排列，避免「个工作日」被「日」截断。
_NUM_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(个工作日|工作日|万元|元|天|日|位|次|小时|%|倍)")


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def extract_quantities(text: str) -> set:
    """抽取「数值+单位」中的数值部分。单位本身不参与比对，避免「30 日 / 30 天」误判。"""
    return {m.group(1) for m in _NUM_UNIT_RE.finditer(text or "")}


def build_citations(index: KnowledgeIndex, raw: Sequence[Dict[str, Any]]) -> List[Citation]:
    citations: List[Citation] = []
    for item in raw or []:
        doc_id = str(item.get("doc_id", "")).strip()
        chunk_id = str(item.get("chunk_id", "")).strip()
        quote = str(item.get("quote", "")).strip()
        pair = index.get_chunk(chunk_id)
        if pair is None:
            citations.append(Citation(doc_id, chunk_id, quote, valid=False, reason="片段不存在"))
            continue
        doc, chunk = pair
        if doc.doc_id != doc_id:
            citations.append(
                Citation(doc_id, chunk_id, quote, valid=False, reason=f"doc_id 与片段不符，实际为 {doc.doc_id}")
            )
            continue
        if len(normalize(quote)) < MIN_QUOTE_LEN:
            citations.append(Citation(doc_id, chunk_id, quote, valid=False, reason="引用过短，无法核验"))
            continue
        if normalize(quote) not in normalize(chunk.text):
            citations.append(Citation(doc_id, chunk_id, quote, valid=False, reason="引用与原文不一致"))
            continue
        citations.append(Citation(doc_id, chunk_id, quote, valid=True))
    return citations


def apply_guardrails(
    index: KnowledgeIndex,
    answer: FinalAnswer,
    question: str,
) -> FinalAnswer:
    """就地修订 FinalAnswer，返回同一个对象，便于调用方直接使用。"""
    # 1) 引用校验
    answer.citations = [c for c in answer.citations]
    invalid = [c for c in answer.citations if not c.valid]
    if invalid:
        for c in invalid:
            answer.warnings.append(f"引用无效（{c.chunk_id}）：{c.reason}")
        answer.citations = [c for c in answer.citations if c.valid]

    if answer.status == "answered" and not answer.citations:
        answer.warnings.append("answered 缺少有效引用，已降级为 insufficient")
        answer.status = "insufficient"
        answer.missing = answer.missing or "没有可核验的原文依据，无法给出有依据的答案"

    # 2) 数值一致性（弱检查）：答案里的每个「数值+单位」，都应在引用原文中出现
    if answer.status == "answered" and answer.citations:
        ans_nums = extract_quantities(answer.answer)
        if ans_nums:
            quote_text = "".join(c.quote for c in answer.citations)
            quote_nums = extract_quantities(quote_text)
            missing = sorted(ans_nums - quote_nums)
            if len(missing) == len(ans_nums):
                answer.warnings.append(
                    f"答案中的数值 {missing} 均未出现在引用原文中，已降级为 insufficient"
                )
                answer.status = "insufficient"
                answer.missing = "答案的关键数值在引用原文中找不到对应，无法确认其依据"
            elif missing:
                answer.warnings.append(f"答案中的数值 {missing} 未出现在引用原文中，请人工复核")

    # 3) 冲突兜底
    if answer.status == "answered":
        cited_docs = sorted({c.doc_id for c in answer.citations})
        conflicts: List[ConflictGroup] = unresolved_among(index, cited_docs)
        q_tokens = set(tokenize(question))
        relevant = [g for g in conflicts if set(tokenize(g.key)) & q_tokens]
        if relevant:
            group = relevant[0]
            answer.warnings.append(f"被引用文档之间存在未消解冲突（{group.key}），已降级为 need_confirmation")
            answer.status = "need_confirmation"
            answer.conflicts = relevant
            answer.answer = (
                f"{answer.answer}\n\n注意：本次引用的文档在「{group.key}」上存在不一致，"
                f"且未声明优先级，无法据此给出唯一结论。{group.reason}。"
            )
    return answer


def summarize_conflicts(groups: Sequence[ConflictGroup]) -> str:
    if not groups:
        return "无冲突"
    return "；".join(f"{g.key}（{'可判定' if g.resolvable else '不可判定'}）" for g in groups)
