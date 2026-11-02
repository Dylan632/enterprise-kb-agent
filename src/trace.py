"""Trace 落盘：JSON（机器可读）+ Markdown（评审可读）。

题面要求执行记录包含「步骤、工具参数、返回摘要、后续动作及简要决策依据」，
这四项在 StepRecord 里逐一对应，渲染时按表输出，便于逐条核对。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from src.models import Trace

STATUS_LABEL = {
    "answered": "已回答",
    "need_confirmation": "待用户确认",
    "insufficient": "资料不足",
    "unfinished": "未完成（达到上限）",
    "error": "工具/模型失败",
}


def to_json(trace: Trace) -> Dict[str, Any]:
    return trace.to_dict()


def save(trace: Trace, out_dir: str | Path) -> Dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "trace.json"
    md_path = out / "trace.md"
    json_path.write_text(json.dumps(trace.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(trace), encoding="utf-8")
    return {"json": json_path, "md": md_path}


def render_markdown(trace: Trace) -> str:
    final = trace.final
    lines = [
        f"# Trace · {trace.question}",
        "",
        f"- run_id: `{trace.run_id}`",
        f"- 模式: `{trace.mode}`",
        f"- 开始时间: {trace.started_at}",
        f"- 耗时: {trace.duration_ms} ms",
        f"- 步数: {len(trace.steps)}",
        f"- 退出原因: `{trace.exit_reason or 'model_submitted'}`",
    ]
    if trace.history:
        lines.append(f"- 对话历史: {len(trace.history)} 轮")
    lines += ["", "## 执行链", "", "| 步 | 工具 | 参数 | 返回摘要 | 下一步 | 决策依据 |", "| --- | --- | --- | --- | --- | --- |"]
    for step in trace.steps:
        args = json.dumps(step.arguments, ensure_ascii=False)
        if len(args) > 120:
            args = args[:117] + "…"
        summary = step.result_summary.replace("|", "\\|")
        if len(summary) > 90:
            summary = summary[:87] + "…"
        reason = (step.reason or "").replace("|", "\\|")
        if len(reason) > 80:
            reason = reason[:77] + "…"
        flag = "" if step.ok else " ❌"
        lines.append(f"| {step.step} | `{step.tool}`{flag} | `{args}` | {summary} | `{step.next_action}` | {reason} |")

    lines += ["", "## 最终结果", ""]
    if final is None:
        lines.append("（无）")
        return "\n".join(lines) + "\n"

    lines += [
        f"**状态**: `{final.status}`（{STATUS_LABEL.get(final.status, final.status)}）",
        "",
        "**回答**",
        "",
        "```text",
        final.answer.strip(),
        "```",
        "",
    ]
    if final.missing:
        lines += [f"**缺失资料**: {final.missing}", ""]
    if final.citations:
        lines += ["**引用**", "", "| doc_id | chunk_id | 是否有效 | 原文片段 |", "| --- | --- | --- | --- |"]
        for c in final.citations:
            quote = c.quote.replace("|", "\\|")
            lines.append(f"| {c.doc_id} | `{c.chunk_id}` | {'✅' if c.valid else '❌ ' + c.reason} | {quote} |")
        lines.append("")
    if final.warnings:
        lines += ["**系统校验告警**", ""]
        lines += [f"- {w}" for w in final.warnings]
        lines.append("")
    return "\n".join(lines) + "\n"
