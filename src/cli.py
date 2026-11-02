"""命令行入口。

评审只需两条命令：
    python -m src.cli ask "报销期限是多久？"
    python -m src.cli eval

ask  单次问答，打印结论与引用；--out 可落盘 trace
eval 跑 data/eval_questions.json 全部用例，产出 examples/<case>/{trace.json,trace.md}
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from src.agent import Agent
from src.index import KnowledgeIndex
from src.llm import build_client
from src.models import Trace
from src.tools import FailureInjector
from src.trace import save as save_trace

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOCS = ROOT / "data" / "docs.json"
DEFAULT_CASES = ROOT / "data" / "eval_questions.json"
DEFAULT_EXAMPLES = ROOT / "examples"

STATUS_LABEL = {
    "answered": "已回答",
    "need_confirmation": "待用户确认",
    "insufficient": "资料不足",
    "unfinished": "未完成（达到上限）",
    "error": "工具/模型失败",
}


def _has_parallel(steps, step_no: int) -> bool:
    return sum(1 for s in steps if s.step == step_no) > 1


def load_index(path: Path) -> KnowledgeIndex:
    if not path.exists():
        raise SystemExit(f"找不到知识库文件：{path}")
    return KnowledgeIndex.from_json(path)


def build_agent(args: argparse.Namespace, index: KnowledgeIndex, extra_fail: List[str] | None = None) -> Agent:
    client = build_client(offline=args.offline)
    fails = set(args.fail_tool or []) | set(extra_fail or [])
    injector = FailureInjector(fail_always=fails)
    return Agent(index, client, max_steps=args.max_steps, injector=injector)


def print_trace(trace: Trace, verbose: bool = False) -> None:
    final = trace.final
    turns = max((s.step for s in trace.steps), default=0)
    print()
    print(f"问题：{trace.question}")
    print(f"模式：{trace.mode}    轮次：{turns}    工具调用：{len(trace.steps)}    耗时：{trace.duration_ms} ms")
    print("-" * 72)
    if verbose:
        seen: dict = {}
        for step in trace.steps:
            seen[step.step] = seen.get(step.step, 0) + 1
            label = f"{step.step}.{seen[step.step]}" if _has_parallel(trace.steps, step.step) else f"{step.step}"
            flag = "  " if step.ok else "❌"
            print(f"{flag} [{label}] {step.tool}({json.dumps(step.arguments, ensure_ascii=False)})")
            print(f"       → {step.result_summary}")
        print("-" * 72)
    if final is None:
        print("（无结果）")
        return
    print(f"状态：{final.status}（{STATUS_LABEL.get(final.status, final.status)}）")
    print()
    print(final.answer.strip())
    if final.missing:
        print()
        print(f"缺失资料：{final.missing}")
    if final.citations:
        print()
        print("引用：")
        for c in final.citations:
            mark = "✓" if c.valid else f"✗ {c.reason}"
            print(f"  [{mark}] {c.doc_id} / {c.chunk_id}：{c.quote}")
    if final.warnings:
        print()
        print("系统校验：")
        for w in final.warnings:
            print(f"  - {w}")


def cmd_ask(args: argparse.Namespace) -> int:
    index = load_index(Path(args.docs))
    agent = build_agent(args, index)

    history: List[Dict[str, str]] = []
    for prior in args.context or []:
        prior_trace = agent.run(prior, history)
        history.append({"question": prior, "answer": prior_trace.final.answer if prior_trace.final else ""})

    trace = agent.run(args.question, history)
    print_trace(trace, verbose=args.verbose)

    if args.out:
        paths = save_trace(trace, Path(args.out))
        print()
        print(f"Trace 已保存：{paths['json']}")
    return 0 if trace.final and trace.final.status in ("answered", "need_confirmation", "insufficient") else 1


def cmd_eval(args: argparse.Namespace) -> int:
    index = load_index(Path(args.docs))
    cases: List[Dict[str, Any]] = json.loads(Path(args.cases).read_text(encoding="utf-8"))["cases"]
    out_root = Path(args.out)

    rows: List[Dict[str, Any]] = []
    for case in cases:
        agent = build_agent(args, index, case.get("fail_tools"))  # 每个用例独立实例，避免故障注入串味
        history: List[Dict[str, str]] = []
        for prior in case.get("history") or []:
            prior_trace = agent.run(prior, history)
            history.append({"question": prior, "answer": prior_trace.final.answer if prior_trace.final else ""})

        trace = agent.run(case["question"], history)
        paths = save_trace(trace, out_root / case["case_dir"])

        final = trace.final
        cited_docs = {c.doc_id for c in (final.citations if final else [])}
        cited_chunks = {c.chunk_id for c in (final.citations if final else [])}
        status_ok = bool(final) and final.status == case["expect_status"]
        docs_ok = all(d in cited_docs for d in case.get("expect_doc_ids", []))
        forbid_docs = [d for d in case.get("forbid_doc_ids", []) if d in cited_docs]
        forbid_chunks = [c for c in case.get("forbid_chunks", []) if c in cited_chunks]
        passed = status_ok and docs_ok and not forbid_docs and not forbid_chunks

        rows.append(
            {
                "id": case["id"],
                "case": case["case_dir"],
                "expect": case["expect_status"],
                "actual": final.status if final else "none",
                "turns": max((s.step for s in trace.steps), default=0),
                "calls": len(trace.steps),
                "cited": ",".join(sorted(cited_docs)) or "-",
                "passed": passed,
                "reason": _fail_reason(status_ok, docs_ok, forbid_docs, forbid_chunks, case),
                "path": _rel(paths["md"]),
            }
        )

    print()
    print(f"{'用例':<6}{'场景':<22}{'期望':<18}{'实际':<18}{'轮':<4}{'调用':<5}{'引用':<12}{'结果'}")
    print("-" * 100)
    for row in rows:
        mark = "PASS" if row["passed"] else "FAIL"
        print(
            f"{row['id']:<6}{row['case']:<22}{row['expect']:<18}{row['actual']:<18}"
            f"{row['turns']:<4}{row['calls']:<5}{row['cited']:<12}{mark} {row['reason']}"
        )
    passed = sum(1 for r in rows if r["passed"])
    print("-" * 100)
    print(f"通过 {passed}/{len(rows)}；trace 输出目录：{out_root}")

    if args.summary:
        Path(args.summary).write_text(
            json.dumps({"passed": passed, "total": len(rows), "rows": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0 if passed == len(rows) else 1


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _fail_reason(status_ok: bool, docs_ok: bool, forbid_docs: List[str], forbid_chunks: List[str],
                 case: Dict[str, Any]) -> str:
    reasons = []
    if not status_ok:
        reasons.append(f"状态不符（期望 {case['expect_status']}）")
    if not docs_ok:
        reasons.append(f"缺少引用 {case.get('expect_doc_ids')}")
    if forbid_docs:
        reasons.append(f"错误引用 {forbid_docs}")
    if forbid_chunks:
        reasons.append(f"错误引用片段 {forbid_chunks}")
    return "；".join(reasons)


def cmd_stats(args: argparse.Namespace) -> int:
    index = load_index(Path(args.docs))
    stats = index.stats()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    for doc in index.docs.values():
        print(f"  {doc.label}  {len(doc.chunks)} 段  {doc.status}")
    return 0


def _add_common(p: argparse.ArgumentParser) -> None:
    """公共参数挂在各子命令上，`python -m src.cli ask "..." --offline` 即可生效。"""
    p.add_argument("--docs", default=str(DEFAULT_DOCS), help="知识库文件路径")
    p.add_argument("--offline", action="store_true", help="强制使用离线模拟器")
    p.add_argument("--max-steps", type=int, default=8, help="步数上限，默认 8")
    p.add_argument("--fail-tool", action="append", default=[], help="注入工具故障，可重复")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kb-agent", description="企业知识问答 Agent")
    sub = parser.add_subparsers(dest="command", required=True)

    ask = sub.add_parser("ask", help="单次问答")
    ask.add_argument("question")
    ask.add_argument("--context", action="append", default=[], help="前置对话，可重复")
    ask.add_argument("--out", help="trace 输出目录")
    ask.add_argument("--verbose", action="store_true", help="打印每一步")
    _add_common(ask)
    ask.set_defaults(func=cmd_ask)

    ev = sub.add_parser("eval", help="跑全部验收用例")
    ev.add_argument("--cases", default=str(DEFAULT_CASES))
    ev.add_argument("--out", default=str(DEFAULT_EXAMPLES))
    ev.add_argument("--summary", help="把结果写成 JSON")
    _add_common(ev)
    ev.set_defaults(func=cmd_eval)

    st = sub.add_parser("stats", help="查看知识库规模")
    _add_common(st)
    st.set_defaults(func=cmd_stats)
    return parser


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
