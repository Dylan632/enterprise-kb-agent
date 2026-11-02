"""跨模块共享的数据结构。

集中声明，避免在 agent / tools / trace 之间传裸 dict。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

# 最终状态：直接对应题面的四种"必须验收"结局
# answered          正常回答
# need_confirmation 存在未消解冲突，需用户确认
# insufficient      资料不足或问题超出知识库范围
# unfinished        达到步数/预算上限，任务未完成
# error             工具失败导致无法完成
Status = str

VALID_STATUSES = ("answered", "need_confirmation", "insufficient", "unfinished", "error")


@dataclass
class Chunk:
    """文档片段，是检索与引用的最小单位。"""

    doc_id: str
    chunk_id: str
    heading: str
    text: str


@dataclass
class Fact:
    """从文档中预抽取的结构化事实，用于跨文档冲突检测。

    合成语料规模小，事实在构建期抽取即可；真实场景应换成抽取模型 + 人工校验。
    """

    key: str
    value: str
    chunk_id: str
    numeric: Optional[float] = None
    unit: str = ""


@dataclass
class Doc:
    doc_id: str
    title: str
    owner: str
    version: str
    effective_date: str
    scope: str
    chunks: List[Chunk]
    facts: List[Fact] = field(default_factory=list)
    status: str = "active"
    supersedes: Optional[str] = None
    superseded_by: Optional[str] = None
    note: str = ""

    @property
    def label(self) -> str:
        return f"{self.doc_id}《{self.title}》{self.version}（{self.effective_date} 生效）"


@dataclass
class SearchHit:
    doc_id: str
    doc_title: str
    chunk_id: str
    heading: str
    snippet: str
    score: float
    coverage: float
    hit_quality: str  # strong / weak / none
    doc_status: str = "active"


@dataclass
class Citation:
    doc_id: str
    chunk_id: str
    quote: str
    valid: bool = True
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ConflictEntry:
    doc_id: str
    doc_title: str
    version: str
    effective_date: str
    value: str
    chunk_id: str
    status: str


@dataclass
class ConflictGroup:
    key: str
    entries: List[ConflictEntry]
    resolvable: bool
    winner_doc_id: Optional[str]
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "resolvable": self.resolvable,
            "winner_doc_id": self.winner_doc_id,
            "reason": self.reason,
            "entries": [asdict(e) for e in self.entries],
        }


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class ToolResult:
    call_id: str
    name: str
    ok: bool
    payload: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    summary: str = ""
    latency_ms: int = 0


@dataclass
class StepRecord:
    """Trace 的一步。字段对齐题面要求的「步骤、工具参数、返回摘要、后续动作、决策依据」。"""

    step: int
    thought: str
    tool: str
    arguments: Dict[str, Any]
    result_summary: str
    next_action: str
    reason: str
    ok: bool = True
    latency_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FinalAnswer:
    status: Status
    answer: str
    citations: List[Citation] = field(default_factory=list)
    conflicts: List[ConflictGroup] = field(default_factory=list)
    missing: str = ""
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "answer": self.answer,
            "citations": [c.to_dict() for c in self.citations],
            "conflicts": [c.to_dict() for c in self.conflicts],
            "missing": self.missing,
            "warnings": self.warnings,
        }


@dataclass
class Trace:
    run_id: str
    question: str
    mode: str
    model: str
    steps: List[StepRecord] = field(default_factory=list)
    final: Optional[FinalAnswer] = None
    history: List[Dict[str, str]] = field(default_factory=list)
    started_at: str = ""
    duration_ms: int = 0
    exit_reason: str = ""
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "question": self.question,
            "mode": self.mode,
            "model": self.model,
            "history": self.history,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "exit_reason": self.exit_reason,
            "steps": [s.to_dict() for s in self.steps],
            "final": self.final.to_dict() if self.final else None,
            "notes": self.notes,
        }
