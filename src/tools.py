"""工具层：模型能调用的全部动作。

只暴露 4 个工具，刻意保持窄：
  search_docs      检索片段（返回 score / coverage / hit_quality，让模型自己判断相关度）
  read_chunk       读取片段原文（引用只能来自这里，避免用 snippet 编造原文）
  check_conflicts  跨文档冲突检测
  submit_answer    提交结构化答案，是循环的唯一正常出口

工具失败由 FailureInjector 注入，用于验证「工具失败」场景。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Set

from src.conflict import conflicts_for_query
from src.index import KnowledgeIndex
from src.models import ToolResult


class FailureInjector:
    """故障注入。fail_always 每次必失败；fail_first 仅首次失败，用于观察模型重试。"""

    def __init__(self, fail_always: Optional[Set[str]] = None, fail_first: Optional[Set[str]] = None):
        self.fail_always = fail_always or set()
        self.fail_first = set(fail_first or set())
        self._seen: Set[str] = set()

    def should_fail(self, tool: str) -> bool:
        if tool in self.fail_always:
            return True
        if tool in self.fail_first and tool not in self._seen:
            self._seen.add(tool)
            return True
        return False


TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_docs",
            "description": (
                "在企业制度知识库中检索相关片段。返回 doc_id、chunk_id、标题、40 字片段、"
                "score、coverage（查询词覆盖率）和 hit_quality（strong/weak/none）。"
                "hit_quality 为 none 时说明没有相关结果，应改写查询后重试或判定资料不足。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索词，使用制度文档中的书面表达效果更好"},
                    "top_k": {"type": "integer", "description": "返回条数，默认 3，最大 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_chunk",
            "description": (
                "读取指定片段的完整原文及所属文档元信息（版本、生效日期、是否已被替代）。"
                "引用原文前必须先调用本工具，不得凭 search_docs 的片段作答。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"chunk_id": {"type": "string", "description": "形如 D01#2"}},
                "required": ["chunk_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_conflicts",
            "description": (
                "检测与查询相关的跨文档冲突。返回每个冲突键下的取值、来源文档、"
                "以及 resolvable 标记：true 表示可通过替代关系/生效日期判定，false 表示无法判定、需向用户确认。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "要检查冲突的主题"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_answer",
            "description": (
                "提交最终答案并结束任务。这是唯一正常出口。"
                "answered 必须附带 citations（doc_id + chunk_id + quote，quote 必须与原文逐字一致）；"
                "need_confirmation 用于冲突无法判定；insufficient 用于知识库无相关资料；"
                "error 用于工具失败导致无法读取原文。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["answered", "need_confirmation", "insufficient", "error"],
                    },
                    "answer": {"type": "string", "description": "面向用户的答复，简洁、不含思维过程"},
                    "citations": {
                        "type": "array",
                        "description": "引用列表，status=answered 时必填",
                        "items": {
                            "type": "object",
                            "properties": {
                                "doc_id": {"type": "string"},
                                "chunk_id": {"type": "string"},
                                "quote": {"type": "string", "description": "从原文逐字复制的支撑句"},
                            },
                            "required": ["doc_id", "chunk_id", "quote"],
                        },
                    },
                    "missing": {"type": "string", "description": "status=insufficient 时说明缺什么资料"},
                },
                "required": ["status", "answer"],
            },
        },
    },
]


class ToolRegistry:
    def __init__(self, index: KnowledgeIndex, injector: Optional[FailureInjector] = None):
        self.index = index
        self.injector = injector or FailureInjector()
        self.submitted: Optional[Dict[str, Any]] = None
        self.call_count: Dict[str, int] = {}

    def definitions(self) -> List[Dict[str, Any]]:
        return TOOL_DEFINITIONS

    def execute(self, name: str, arguments: Dict[str, Any]) -> ToolResult:
        started = time.perf_counter()
        self.call_count[name] = self.call_count.get(name, 0) + 1

        if self.injector.should_fail(name):
            result = ToolResult(
                call_id="",
                name=name,
                ok=False,
                error=f"{name} 调用失败：上游服务超时（模拟故障注入）",
                summary=f"{name} 失败：上游服务超时",
            )
            result.latency_ms = int((time.perf_counter() - started) * 1000)
            return result

        handler = {
            "search_docs": self._search_docs,
            "read_chunk": self._read_chunk,
            "check_conflicts": self._check_conflicts,
            "submit_answer": self._submit_answer,
        }.get(name)

        if handler is None:
            result = ToolResult(call_id="", name=name, ok=False, error=f"未知工具：{name}", summary="未知工具")
        else:
            try:
                result = handler(arguments)
            except Exception as exc:  # 工具内部异常不能击穿 Agent 循环
                result = ToolResult(call_id="", name=name, ok=False, error=f"{name} 执行异常：{exc}", summary="执行异常")
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    # ---------- 各工具实现 ----------

    def _search_docs(self, args: Dict[str, Any]) -> ToolResult:
        query = str(args.get("query", "")).strip()
        top_k = min(int(args.get("top_k", 3) or 3), 5)
        if not query:
            return ToolResult(call_id="", name="search_docs", ok=False, error="query 不能为空", summary="参数错误")
        hits = self.index.search(query, top_k=top_k)
        payload = {
            "query": query,
            "hits": [
                {
                    "doc_id": h.doc_id,
                    "doc_title": h.doc_title,
                    "chunk_id": h.chunk_id,
                    "heading": h.heading,
                    "snippet": h.snippet,
                    "score": h.score,
                    "coverage": h.coverage,
                    "hit_quality": h.hit_quality,
                    "doc_status": h.doc_status,
                }
                for h in hits
            ],
        }
        if not hits:
            summary = "检索结果为空（0 条）"
        else:
            best = hits[0]
            summary = (
                f"检索「{query}」返回 {len(hits)} 条，最佳 {best.chunk_id}"
                f"（{best.heading}，coverage={best.coverage}，{best.hit_quality}）"
            )
        return ToolResult(call_id="", name="search_docs", ok=True, payload=payload, summary=summary)

    def _read_chunk(self, args: Dict[str, Any]) -> ToolResult:
        chunk_id = str(args.get("chunk_id", "")).strip()
        pair = self.index.get_chunk(chunk_id)
        if pair is None:
            return ToolResult(
                call_id="",
                name="read_chunk",
                ok=False,
                error=f"片段不存在：{chunk_id}",
                summary=f"read_chunk 失败：{chunk_id} 不存在",
            )
        doc, chunk = pair
        payload = {
            "doc_id": doc.doc_id,
            "doc_title": doc.title,
            "version": doc.version,
            "effective_date": doc.effective_date,
            "status": doc.status,
            "supersedes": doc.supersedes,
            "superseded_by": doc.superseded_by,
            "owner": doc.owner,
            "scope": doc.scope,
            "chunk_id": chunk.chunk_id,
            "heading": chunk.heading,
            "text": chunk.text,
        }
        return ToolResult(
            call_id="",
            name="read_chunk",
            ok=True,
            payload=payload,
            summary=f"读取 {chunk.chunk_id}《{doc.title}》{chunk.heading}，{len(chunk.text)} 字",
        )

    def _check_conflicts(self, args: Dict[str, Any]) -> ToolResult:
        query = str(args.get("query", "")).strip()
        groups = conflicts_for_query(self.index, query)
        payload = {
            "query": query,
            "conflict_count": len(groups),
            "conflicts": [g.to_dict() for g in groups],
        }
        if not groups:
            summary = "未发现相关冲突"
        else:
            parts = [
                f"{g.key}（{'可判定，以 ' + g.winner_doc_id + ' 为准' if g.resolvable else '不可判定，需确认'}）"
                for g in groups
            ]
            summary = f"发现 {len(groups)} 组冲突：" + "；".join(parts)
        return ToolResult(call_id="", name="check_conflicts", ok=True, payload=payload, summary=summary)

    def _submit_answer(self, args: Dict[str, Any]) -> ToolResult:
        self.submitted = args
        status = args.get("status", "answered")
        citations = args.get("citations") or []
        return ToolResult(
            call_id="",
            name="submit_answer",
            ok=True,
            payload={"status": status, "citation_count": len(citations)},
            summary=f"提交答案 status={status}，引用 {len(citations)} 条",
        )
