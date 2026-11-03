"""模型接入层。

两种实现，接口一致：

  OpenAICompatibleClient  真实模型（DeepSeek / 通义 / 任意 OpenAI 兼容端点）
  OfflineScriptedClient   离线脚本化模拟器

离线模拟器的定位必须说清楚：它用规则复现「检索→改写→读取→查冲突→提交」这条
决策链，用来在没有 API key 时验证工程链路、Trace 结构与引用校验是否工作。
它**不能**证明真实模型的决策质量——题面对 Mock 的边界有明确要求，README 与
docs/03-evaluation.md 都会重复这一点。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.conflict import is_conflict_relevant
from src.index import tokenize
from src.models import ToolCall


class LLMError(RuntimeError):
    pass


@dataclass
class ChatResponse:
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


class LLMClient:
    name: str = "base"

    def chat(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]]) -> ChatResponse:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# 真实模型
# --------------------------------------------------------------------------- #


class OpenAICompatibleClient(LLMClient):
    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 60, temperature: float = 0.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.name = f"live:{model}"

    def chat(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]]) -> ChatResponse:
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")[:500]
            raise LLMError(f"模型接口返回 {exc.code}：{detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LLMError(f"模型接口不可达：{exc}") from exc

        choice = (raw.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls: List[ToolCall] = []
        for idx, item in enumerate(message.get("tool_calls") or []):
            fn = item.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {"_raw": raw_args}
            tool_calls.append(ToolCall(id=item.get("id") or f"call_{idx}", name=fn.get("name", ""), arguments=args))

        usage = raw.get("usage") or {}
        return ChatResponse(
            content=message.get("content") or "",
            tool_calls=tool_calls,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )


# --------------------------------------------------------------------------- #
# 离线模拟器
# --------------------------------------------------------------------------- #

# 口语 → 书面表达的改写规则。真实模型自己会做这一步，这里只是模拟器的手写替身。
_REWRITE_RULES: List[Tuple[Tuple[str, ...], str]] = [
    (("酒店", "宾馆", "住一晚", "住宿一晚"), "出差住宿标准"),
    (("能报多少", "多少钱", "上限多少", "报销额度"), "报销标准"),
    (("怎么请", "怎么申请", "如何申请"), "申请流程"),
    (("密码", "口令"), "密码策略"),
    (("比价", "货比三家"), "采购比价要求"),
]

_DEMONSTRATIVES = ("那", "这", "它", "该", "上述", "上面", "刚才")


class OfflineScriptedClient(LLMClient):
    """规则驱动的模拟器。每一步都从 tool 消息里读观察结果，再决定下一步。"""

    name = "offline:scripted"

    def __init__(self, max_rewrites: int = 2):
        self.max_rewrites = max_rewrites

    # ---------- 对外接口 ----------

    def chat(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]]) -> ChatResponse:
        users = [m.get("content", "") for m in messages if m.get("role") == "user"]
        question = users[-1] if users else ""
        history = users[:-1]
        observations = self._observations(messages)

        searches = [o for o in observations if o[0] == "search_docs"]
        reads = [o for o in observations if o[0] == "read_chunk"]
        checks = [o for o in observations if o[0] == "check_conflicts"]

        # 1) 首次检索（含指代消解）
        if not searches:
            query = self._expand_followup(question, history)
            thought = (
                f"问题含指代，结合上一轮问句扩展检索词为「{query}」"
                if query != question
                else f"先检索「{query}」"
            )
            return self._call("search_docs", {"query": query, "top_k": 3}, thought)

        # 2) 无相关结果 → 改写查询；无可改写项则判定资料不足
        last_query = searches[-1][1].get("query", question)
        hits = searches[-1][2].get("hits", [])
        usable = [h for h in hits if h.get("hit_quality") != "none"]
        if not usable:
            rewritten = self._rewrite(last_query) if len(searches) <= self.max_rewrites else None
            if rewritten and rewritten != last_query:
                return self._call(
                    "search_docs",
                    {"query": rewritten, "top_k": 3},
                    f"「{last_query}」无相关命中，改写为「{rewritten}」重试",
                )
            thought = (
                "改写后仍无相关命中，判定资料不足，不作推测"
                if rewritten
                else "检索无相关命中，且没有可尝试的改写方向，判定资料不足，不作推测"
            )
            return self._submit(
                "insufficient",
                f"知识库中没有找到与「{question}」相关的制度内容。现有资料未覆盖该主题，无法回答。",
                missing=f"缺少与「{question}」对应的制度文件或条款",
                thought=thought,
            )

        # 3) 读取最相关片段原文
        tried = {r[1].get("chunk_id") for r in reads}
        ok_reads = [r[2] for r in reads if r[2].get("ok")]
        if not reads:
            top = usable[0]
            return self._call(
                "read_chunk",
                {"chunk_id": top["chunk_id"]},
                f"{top['chunk_id']}（{top['heading']}）相关度最高，读取原文以便逐字引用",
            )
        if not ok_reads:
            candidate = next((h for h in usable if h["chunk_id"] not in tried), None)
            if candidate:
                return self._call(
                    "read_chunk",
                    {"chunk_id": candidate["chunk_id"]},
                    f"上一个片段读取失败，改读 {candidate['chunk_id']} 重试",
                )
            return self._submit(
                "error",
                "读取原文失败，无法核实条款原文，因此不能给出有依据的答案。请稍后重试。",
                thought="read_chunk 连续失败，按规则停止作答",
            )

        # 4) 查冲突
        if not checks:
            return self._call(
                "check_conflicts",
                {"query": last_query},
                "已读到原文，提交前检查是否存在跨文档冲突",
            )

        # 5) 冲突涉及的另一份文件还没读 → 补读，保证并列引用都来自原文
        groups = checks[-1][2].get("conflicts", [])
        read_ids = {p.get("chunk_id") for p in ok_reads}
        for group in groups:
            if not is_conflict_relevant(group["key"], question):
                continue
            pending = [e for e in group["entries"] if e["chunk_id"] not in read_ids]
            if pending and len(ok_reads) < 3:
                target = pending[0]
                return self._call(
                    "read_chunk",
                    {"chunk_id": target["chunk_id"]},
                    f"「{group['key']}」存在冲突，补读 {target['doc_id']} 原文以便并列引用",
                )

        # 6) 组装答案并提交
        return self._compose(question, last_query, ok_reads, checks[-1][2])

    # ---------- 内部工具 ----------

    @staticmethod
    def _observations(messages: List[Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
        """把消息序列还原成 (工具名, 参数, 结果) 三元组。"""
        pending: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        out: List[Tuple[str, Dict[str, Any], Dict[str, Any]]] = []
        for msg in messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    pending[tc.get("id", "")] = (fn.get("name", ""), args)
            elif msg.get("role") == "tool":
                name, args = pending.get(msg.get("tool_call_id", ""), ("", {}))
                try:
                    payload = json.loads(msg.get("content") or "{}")
                except json.JSONDecodeError:
                    payload = {}
                out.append((name, args, payload))
        return out

    def _expand_followup(self, question: str, history: Sequence[str]) -> str:
        if not history:
            return question
        if question[:1] in _DEMONSTRATIVES or any(k in question for k in ("这个", "那个", "上面")):
            return f"{history[-1]} {question}"
        return question

    def _rewrite(self, query: str) -> Optional[str]:
        """只在命中改写规则时返回新查询。

        刻意不做「去掉疑问词再搜一次」这类泛化改写：模拟器无法判断新查询是否仍然
        切题，贸然重试会把「公司年会的预算」这种资料外问题搜出无关片段，
        从而产出一个看起来有依据、实则错误的答案。宁可不改写，也不猜。
        """
        for keys, target in _REWRITE_RULES:
            if any(k in query for k in keys):
                return target
        return None

    def _call(self, name: str, args: Dict[str, Any], thought: str) -> ChatResponse:
        return ChatResponse(
            content=thought,
            tool_calls=[ToolCall(id=f"call_{name}_{abs(hash((name, json.dumps(args, ensure_ascii=False)))) % 100000}", name=name, arguments=args)],
        )

    def _submit(self, status: str, answer: str, citations: Optional[List[Dict[str, str]]] = None,
                missing: str = "", thought: str = "") -> ChatResponse:
        args: Dict[str, Any] = {"status": status, "answer": answer, "citations": citations or []}
        if missing:
            args["missing"] = missing
        return self._call("submit_answer", args, thought or f"提交答案：{status}")

    def _compose(self, question: str, query: str, ok_reads: List[Dict[str, Any]], check_payload: Dict[str, Any]) -> ChatResponse:
        groups = check_payload.get("conflicts", [])
        cited_docs = {p.get("doc_id") for p in ok_reads}

        unresolved = [
            g for g in groups
            if not g.get("resolvable") and is_conflict_relevant(g["key"], question)
        ]
        if unresolved:
            group = unresolved[0]
            lines = [
                f"关于「{group['key']}」，知识库中存在不一致，且无法判定应适用哪一份，需要你确认：",
                "",
            ]
            for entry in group["entries"]:
                lines.append(
                    f"- {entry['doc_id']}《{entry['doc_title']}》（{entry['version']}，"
                    f"{entry['effective_date']} 生效，{entry['status']}）：{entry['value']}"
                )
            lines += ["", f"判定依据：{group['reason']}。", "", "请确认以哪一份文件为准，我再给出最终答复。"]
            citations = [
                {"doc_id": e["doc_id"], "chunk_id": e["chunk_id"], "quote": self._quote_for(e["chunk_id"], ok_reads)}
                for e in group["entries"]
                if e["doc_id"] in cited_docs
            ]
            return self._submit(
                "need_confirmation",
                "\n".join(lines),
                citations=[c for c in citations if c["quote"]],
                thought=f"发现 {group['key']} 冲突且不可判定，提交待确认",
            )

        primary = ok_reads[0]
        sentence = self._pick_sentence(primary.get("text", ""), query)
        if not sentence:
            return self._submit(
                "insufficient",
                f"检索到 {primary.get('chunk_id')}，但内容无法回答该问题。",
                missing="命中的片段与问题不匹配",
                thought="命中片段不支撑回答",
            )

        answer = (
            f"根据 {primary['doc_id']}《{primary['doc_title']}》（{primary['version']}）"
            f"{primary['heading']}：{sentence}"
        )

        # 可判定冲突：只在问的正是这个主题时补一句依据
        for group in groups:
            if not group.get("resolvable"):
                continue
            if not is_conflict_relevant(group["key"], question):
                continue
            if group.get("winner_doc_id") not in cited_docs:
                continue
            answer += f"\n\n（依据：{group['reason']}）"
            break

        return self._submit(
            "answered",
            answer,
            citations=[{"doc_id": primary["doc_id"], "chunk_id": primary["chunk_id"], "quote": sentence}],
            thought="原文已确认且无未消解冲突，提交答案",
        )

    @staticmethod
    def _pick_sentence(text: str, query: str) -> str:
        sentences = [s.strip() for s in re.split(r"(?<=[。；])", text) if s.strip()]
        if not sentences:
            return ""
        q = set(tokenize(query))
        best, best_score = sentences[0], -1.0
        for sentence in sentences:
            score = len(q & set(tokenize(sentence)))
            if score > best_score:
                best, best_score = sentence, score
        return best

    @staticmethod
    def _quote_for(chunk_id: str, ok_reads: List[Dict[str, Any]]) -> str:
        for payload in ok_reads:
            if payload.get("chunk_id") == chunk_id:
                sentences = [s.strip() for s in re.split(r"(?<=[。；])", payload.get("text", "")) if s.strip()]
                return sentences[0] if sentences else payload.get("text", "")
        return ""


def build_client(offline: bool = False) -> LLMClient:
    """按环境变量决定用真实模型还是离线模拟器。"""
    api_key = os.getenv("KB_AGENT_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    base_url = os.getenv("KB_AGENT_BASE_URL", "https://api.deepseek.com/v1")
    model = os.getenv("KB_AGENT_MODEL", "deepseek-flash")
    if offline or not api_key:
        return OfflineScriptedClient()
    return OpenAICompatibleClient(base_url=base_url, api_key=api_key, model=model)
