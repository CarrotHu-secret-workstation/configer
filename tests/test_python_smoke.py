"""Python 适配器冒烟测试（骨架期）：load 成功 + save([]) 字节恒等。"""

from __future__ import annotations

from pathlib import Path

from configer.adapters.python_adapter import PythonAdapter

ROOT = Path(__file__).resolve().parents[1]
PARAM = ROOT / "testdata" / "param.py"


def test_smoke_load_and_identity_save() -> None:
    adapter = PythonAdapter()
    raw = PARAM.read_bytes()
    assert adapter.detect(PARAM, raw[:4096]) is True

    doc, diags = adapter.load(PARAM)
    assert doc.format == "python"
    assert doc.original_bytes == raw
    assert doc.content_hash
    assert not [d for d in diags if d.severity == "error"]
    assert len(doc.items) > 0

    out = adapter.save(doc, [])
    assert out == raw
