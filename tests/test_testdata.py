"""金样本 testdata/ 完整性测试（规范 §0.4：拷贝属实现期动作，须注明来源
路径与规范版本；testdata 为回归测试固定输入，必须保持字节级不动）。

- 与源文件比对：源不存在时 skip（源项目可能不在本机）；
- 与拷贝时点（2026-09-08，规范 v0.4）固化的 sha256 比对：任何对 testdata
  的意外改动（编辑器重排、换行符转换等）都会在此暴露。若源文件演进需更新
  拷贝，必须同步更新本文件与 testdata/README.md 的哈希及规范 §11 计数。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TESTDATA = ROOT / "testdata"

GOLDEN = {
    "param.py": {
        "source": Path(
            "/home/pi/Workspace/Prj/robocup_RLBrain/BehaviorTree_based_code/"
            "sim-3v3-stable-change6o3B/src/param.py"
        ),
        "sha256": "fc409d2afeb472ce94012cb71c29d7e6fde4405e61f34aa24a785ed0fe0f11b8",
    },
    "config.yaml": {
        "source": Path(
            "/home/pi/Workspace/Prj/robocup_physical_demo/K1_5v5_RLBrain_demo/"
            "K1_5v5_Demo_1.5/src/brain/config/config.yaml"
        ),
        "sha256": "73684a2707aafe669efacc7edcb48e7ce5cf5b44cb4c3a7f0b32d767b39b984d",
    },
}


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_testdata_readme_exists() -> None:
    readme = TESTDATA / "README.md"
    assert readme.is_file(), "testdata/README.md 必须存在（§0.4：注明来源路径与规范版本）"
    text = readme.read_text(encoding="utf-8")
    for name, meta in GOLDEN.items():
        assert name in text
        assert str(meta["source"]) in text, f"README 须注明 {name} 的来源绝对路径"
    assert "2026-09-08" in text and "v0.4" in text


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_testdata_matches_pinned_hash(name: str) -> None:
    """testdata 拷贝必须与拷贝时点固化的 sha256 一致（字节级不动）。"""
    copy = TESTDATA / name
    assert copy.is_file(), f"缺少金样本拷贝 {copy}"
    assert sha256_of(copy) == GOLDEN[name]["sha256"], (
        f"testdata/{name} 与金样本固化哈希不一致：该文件必须保持字节级不动"
    )


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_testdata_matches_source(name: str) -> None:
    """与源文件比对；源不在本机时 skip。"""
    source: Path = GOLDEN[name]["source"]
    if not source.is_file():
        pytest.skip(f"金样本源文件不存在：{source}")
    assert sha256_of(TESTDATA / name) == sha256_of(source), (
        f"testdata/{name} 与源文件 {source} 不一致"
    )
