"""configer.registry 单元测试（规范 §4.2 注册表与格式嗅探全分支）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from configer.model import CODE_E_FORMAT, SEV_ERROR
from configer.registry import Registry, default_registry


class FakeAdapter:
    """§4.1 Adapter 接口的测试替身：记录 detect 调用，可配置嗅探结果。"""

    def __init__(self, name: str, extensions: tuple[str, ...], detect_result: bool | Exception = False):
        self.name = name
        self.extensions = extensions
        self.detect_result = detect_result
        self.detect_calls: list[tuple[Path, bytes]] = []

    def detect(self, path: Path, head: bytes) -> bool:
        self.detect_calls.append((path, head))
        if isinstance(self.detect_result, Exception):
            raise self.detect_result
        return self.detect_result

    def load(self, path):  # pragma: no cover - 嗅探测试不触达
        raise NotImplementedError

    def save(self, doc, edits):  # pragma: no cover - 嗅探测试不触达
        raise NotImplementedError


def make_registry() -> tuple[Registry, FakeAdapter, FakeAdapter]:
    py = FakeAdapter("python", (".py",))
    yml = FakeAdapter("yaml", (".yaml", ".yml"))
    reg = Registry()
    reg.register(py)
    reg.register(yml)
    return reg, py, yml


# ---------------------------------------------------------------------------
# 注册表基础
# ---------------------------------------------------------------------------

def test_registry_is_ordered() -> None:
    reg, py, yml = make_registry()
    assert [a.name for a in reg.adapters] == ["python", "yaml"]  # 有序（§4.2）


# ---------------------------------------------------------------------------
# default_registry 接线（chunk 3c，§4.2：v1 固定顺序 [Python, Yaml]）
# ---------------------------------------------------------------------------

TESTDATA = Path(__file__).resolve().parent.parent / "testdata"


def test_default_registry_registers_python_then_yaml() -> None:
    from configer.adapters.python_adapter import PythonAdapter
    from configer.adapters.yaml_adapter import YamlAdapter

    reg = default_registry()
    assert [a.name for a in reg.adapters] == ["python", "yaml"]
    assert isinstance(reg.adapters[0], PythonAdapter)
    assert isinstance(reg.adapters[1], YamlAdapter)
    # 每次调用新建实例（无共享可变状态）
    assert default_registry() is not reg


def test_default_registry_extension_selection() -> None:
    reg = default_registry()
    assert reg.select_adapter(Path("/x/param.py"), b"")[0].name == "python"
    assert reg.select_adapter(Path("/x/a.yaml"), b"")[0].name == "yaml"
    assert reg.select_adapter(Path("/x/a.yml"), b"")[0].name == "yaml"
    # 显式 format 按注册名选定（A-10 前置：--format yaml 开 .py 选中 yaml）
    assert reg.select_adapter(Path("/x/param.py"), b"", "yaml")[0].name == "yaml"
    assert reg.select_adapter(Path("/x/a.yaml"), b"", "python")[0].name == "python"


def test_default_registry_sniff_without_extension() -> None:
    # 内容嗅探只走 detect（不触 save，避开并行开发半成品）；head=全文件字节
    # （与 SessionManager.open_files 的取法一致）
    reg = default_registry()
    yaml_bytes = (TESTDATA / "config.yaml").read_bytes()
    py_bytes = (TESTDATA / "param.py").read_bytes()
    # yaml mapping 文本（含缩进嵌套，非合法 python）→ yaml
    assert reg.select_adapter(Path("/x/noext_yaml"), yaml_bytes)[0].name == "yaml"
    # python 源（顶层非 mapping，ruamel 组合失败）→ python
    assert reg.select_adapter(Path("/x/noext_py"), py_bytes)[0].name == "python"


def test_default_registry_all_sniff_fail_gives_e_format() -> None:
    reg = default_registry()
    adapter, diags = reg.select_adapter(
        Path("/x/data.bin"), b"\x00\xff\xfe\x80\x81 not text"
    )
    assert adapter is None
    assert len(diags) == 1
    assert diags[0].severity == SEV_ERROR and diags[0].code == CODE_E_FORMAT


def test_register_duplicate_name_rejected() -> None:
    reg, py, _ = make_registry()
    with pytest.raises(ValueError):
        reg.register(FakeAdapter("python", (".py",)))


def test_get_by_name() -> None:
    reg, py, yml = make_registry()
    assert reg.get("yaml") is yml
    assert reg.get("toml") is None


# ---------------------------------------------------------------------------
# auto：扩展名命中唯一 → 直接选定（嗅探确认由调用方负责，§4.2）
# ---------------------------------------------------------------------------

def test_auto_extension_unique_match() -> None:
    reg, py, yml = make_registry()
    adapter, diags = reg.select_adapter(Path("/x/param.py"), b"", "auto")
    assert adapter is py and diags == []
    # 扩展名命中即选定，不调用 detect（确认环节在调用方的 load）
    assert py.detect_calls == [] and yml.detect_calls == []


def test_auto_extension_match_second_adapter() -> None:
    reg, py, yml = make_registry()
    assert reg.select_adapter(Path("/x/config.yaml"), b"")[0] is yml
    assert reg.select_adapter(Path("/x/config.yml"), b"")[0] is yml


def test_auto_extension_match_case_insensitive() -> None:
    reg, py, yml = make_registry()
    assert reg.select_adapter(Path("/x/CONFIG.YAML"), b"")[0] is yml


def test_auto_format_hint_defaults_to_auto() -> None:
    reg, py, _ = make_registry()
    assert reg.select_adapter(Path("/x/a.py"), b"")[0] is py


# ---------------------------------------------------------------------------
# auto：扩展名未命中 → 按注册顺序逐个 detect
# ---------------------------------------------------------------------------

def test_auto_no_extension_match_falls_to_detect_in_order() -> None:
    a = FakeAdapter("python", (".py",), detect_result=False)
    b = FakeAdapter("yaml", (".yaml",), detect_result=True)
    reg = Registry()
    reg.register(a)
    reg.register(b)
    adapter, diags = reg.select_adapter(Path("/x/data.txt"), b"head", "auto")
    assert adapter is b and diags == []
    # 按注册顺序逐个 detect，且 head 原样传入
    assert a.detect_calls == [(Path("/x/data.txt"), b"head")]
    assert b.detect_calls == [(Path("/x/data.txt"), b"head")]


def test_auto_detect_stops_at_first_hit() -> None:
    a = FakeAdapter("python", (".py",), detect_result=True)
    b = FakeAdapter("yaml", (".yaml",), detect_result=True)
    reg = Registry()
    reg.register(a)
    reg.register(b)
    adapter, _ = reg.select_adapter(Path("/x/unknown"), b"", "auto")
    assert adapter is a
    assert b.detect_calls == []  # 命中即停，后续不再嗅探


def test_auto_detect_raising_adapter_treated_as_false() -> None:
    # detect 不得抛异常（§4.1）；注册表防御性捕获，坏适配器不崩嗅探流程
    bad = FakeAdapter("python", (".py",), detect_result=RuntimeError("boom"))
    good = FakeAdapter("yaml", (".yaml",), detect_result=True)
    reg = Registry()
    reg.register(bad)
    reg.register(good)
    adapter, diags = reg.select_adapter(Path("/x/unknown"), b"", "auto")
    assert adapter is good and diags == []


# ---------------------------------------------------------------------------
# auto：全部失败 → E-FORMAT
# ---------------------------------------------------------------------------

def test_auto_all_detect_fail_gives_e_format() -> None:
    reg, py, yml = make_registry()  # 两者 detect_result 默认 False
    adapter, diags = reg.select_adapter(Path("/x/data.bin"), b"??", "auto")
    assert adapter is None
    assert len(diags) == 1
    d = diags[0]
    assert d.severity == SEV_ERROR and d.code == CODE_E_FORMAT == "E-FORMAT"
    assert "data.bin" in d.message  # 可定位信息（§3.7）
    assert len(py.detect_calls) == 1 and len(yml.detect_calls) == 1


def test_auto_empty_registry_gives_e_format() -> None:
    # 裸空注册表（default_registry 已接线为 [python, yaml]，不再为空）
    adapter, diags = Registry().select_adapter(Path("/x/a.py"), b"", "auto")
    assert adapter is None
    assert diags[0].code == CODE_E_FORMAT


# ---------------------------------------------------------------------------
# 显式 format_hint：按 name 直接选定 / 未注册 → E-FORMAT（§4.2、§9.1）
# ---------------------------------------------------------------------------

def test_explicit_format_selects_by_name_ignoring_extension() -> None:
    reg, py, yml = make_registry()
    # --format yaml 打开 .py 文件：直接选定 yaml（其后 load 失败由调用方按
    # §9.1 exit 2 处理，不得回退其他适配器）
    adapter, diags = reg.select_adapter(Path("/x/param.py"), b"", "yaml")
    assert adapter is yml and diags == []
    assert py.detect_calls == [] and yml.detect_calls == []
    assert reg.select_adapter(Path("/x/config.yaml"), b"", "python")[0] is py


def test_explicit_format_unregistered_gives_e_format() -> None:
    reg, _, _ = make_registry()
    adapter, diags = reg.select_adapter(Path("/x/a.toml"), b"", "toml")
    assert adapter is None
    assert len(diags) == 1
    assert diags[0].severity == SEV_ERROR and diags[0].code == CODE_E_FORMAT
    assert "toml" in diags[0].message


def test_explicit_format_on_empty_registry_gives_e_format() -> None:
    # 裸空注册表：显式名也未注册 → E-FORMAT
    adapter, diags = Registry().select_adapter(Path("/x/a.py"), b"", "python")
    assert adapter is None and diags[0].code == CODE_E_FORMAT
