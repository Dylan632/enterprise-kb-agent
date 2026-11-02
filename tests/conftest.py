"""pytest 公共配置：把项目根目录加入 sys.path，使 `from src.*` 可用。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.index import KnowledgeIndex  # noqa: E402


@pytest.fixture(scope="session")
def index() -> KnowledgeIndex:
    return KnowledgeIndex.from_json(ROOT / "data" / "docs.json")
