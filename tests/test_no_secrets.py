"""提交前自检：仓库内不得出现真实密钥。

这条测试不是形式主义。`.env` 虽然在 .gitignore 里，但一次 `git add -f`、
一次把 key 粘进 README、或者把 key 写进 trace 的 note 字段，都会造成泄漏。
把它放进 pytest，`python -m pytest` 就是提交前的最后一道闸。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules"}
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".zip", ".pyc"}

# 常见密钥形态：OpenAI/DeepSeek 风格、AWS、私钥头
PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def _iter_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIX:
            continue
        yield path


def test_no_hardcoded_secret_in_repo():
    hits = []
    for path in _iter_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in PATTERNS:
            for match in pattern.finditer(text):
                snippet = match.group()[:8] + "…"
                hits.append(f"{path.relative_to(ROOT)}: {snippet}")
    assert not hits, "发现疑似真实密钥，请移除后再提交：\n" + "\n".join(hits)


def test_env_example_has_no_key():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("KB_AGENT_API_KEY="):
            assert line.strip() == "KB_AGENT_API_KEY=", ".env.example 中的 key 必须留空"
            return
    pytest.fail(".env.example 缺少 KB_AGENT_API_KEY 占位")


def test_env_is_gitignored():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gitignore
    assert ".env.*" in gitignore
    assert "!.env.example" in gitignore
