# configer

> 配置文件 GUI 编辑器：语义模型 + 适配器（规范 [`docs/spec.md`](docs/spec.md) v0.4）

一个面向机器人/工程项目的配置编辑工具——既能编辑 **Python 模块级常量配置**（`param.py`），
也能编辑 **深层嵌套 YAML 配置**（ROS 2 风格 `config.yaml`），用同一套语义模型驱动。

---

## ✦ 它在做什么

工程项目里配置文件的现实是**两种风格并存**：

- 某些项目用 Python 模块级大写常量当配置（`KICK_POWER = 8.92`）—— 好读、好 diff、Python 程序直接 `import`。
- 另一些项目用深层嵌套 YAML（ROS 2 / Hydra / 自家 DSL）—— 适合复杂结构、运行时加载。

`configer` 不是一个"通用 YAML 编辑器"，而是把这两类文件**统一映射到一套语义模型**：

```
                 ┌─────────────────┐
   YAML 文件 ──▶ │  YamlAdapter    │ ─┐
                 └─────────────────┘  │
                 ┌─────────────────┐  ├─▶ ConfigDoc ─▶ GUI ─▶ EditOp ─▶ 写回
Python 文件 ──▶ │ PythonAdapter   │ ─┘                       （round-trip 保真）
                 └─────────────────┘
```

核心只懂语义模型（§3），不解析任何格式；每种格式由一个**适配器**负责解析与写回。

---

## ✦ 核心理念（规范 §0.1）

1. **不存在统一文件格式**——核心是语义模型，格式是适配器的私事。
2. **无损写回是生死线**——`git diff` 只出现用户编辑的那一行，未触碰行字节级不变。
3. **注释即文档**——从注释里提取说明 / 枚举候选 / 范围约束，对老文件零迁移成本。
4. **推测不作铁律**——注释启发式推出的约束只黄标不拦截；只有显式声明才红标拦截。

---

## ✦ 功能

- **多文件并行编辑**——侧栏列出所有打开的文件，可同时编辑多个
- **两种格式**——YAML（ruamel.yaml round-trip）+ Python（libcst CST 级编辑）
- **语义模型驱动**——条目按分组展示，含类型 / 当前值 / 枚举候选 / 范围约束 / 说明段 / 只读原因
- **注释画像**——YAML 行尾注释 `red | blue | green` 自动推断枚举；Python `# 作用 / 影响 / 建议 [min, max]` 自动推断范围
- **结构化标注**（v1.5）——`@type=@min=@max=@enum=@unit=@desc=` 显式声明硬约束
- **实时落盘**——编辑即时提交（防抖 400ms），外部轮询检测文件变更，原子写防中途崩溃
- **单实例 + 转交**——`configer open` 第二次调用自动把文件路径转交给已运行的实例
- **门控校验**——类型错误 / declared 违规 → 红标拦截；inferred 违规 → 黄标放行（I-6）
- **保真写回**——未编辑行字节级不变；浮点字面量风格保留（`0.15`/`100.`/`0.90` 各按原样回写）

---

## ✦ 安装

```bash
pip install -e .
# 或
pip install libcst ruamel.yaml nicegui
```

依赖（见 `pyproject.toml`）：

- Python ≥ 3.10
- `libcst` —— Python CST 解析/编辑
- `ruamel.yaml` —— YAML round-trip
- `nicegui` —— GUI（基于 Vue.js 的 Python web 框架，本地启动浏览器）

---

## ✦ 使用

### CLI

```bash
# 打开一个或多个文件（自动嗅探格式）
configer open config.yaml
configer open config.yaml param.py
configer open *.yaml --format yaml

# 查看版本
configer --version
```

完整契约见规范 §9。

### 作为库

```python
from pathlib import Path
from configer.registry import default_registry
from configer.model import EditOp

reg = default_registry()
adapter, _ = reg.select_adapter(Path("config.yaml"), head=b"...", format_hint="auto")
doc, diagnostics = adapter.load(Path("config.yaml"))
doc.refresh_hash()

# 查看语义模型
for it in doc.items:
    print(f"{it.path} = {it.value} ({it.type})  enum={[c.value for c in it.enum_candidates]}")

# 修改并写回
new_bytes = adapter.save(doc, [EditOp(path="robot.speed_limit", new_value=75)])
Path("config.yaml").write_bytes(new_bytes)  # 未编辑行字节级不变
```

完整 API 见规范 §3 / §4 / §7。

---

## ✦ 架构（三层）

| 层 | 模块 | 职责 |
|---|---|---|
| **前端** | `src/configer/ui/`、`src/configer/cli.py` | 渲染 / 命令行 / 事件循环 |
| **核心语义模型** | `src/configer/model.py`、`src/configer/core/{session,validation,poller,writeback,checks}.py` | ConfigDoc / EditOp / 门控 / 原子写 / 外部轮询 |
| **适配器** | `src/configer/adapters/{yaml_adapter,python_adapter}.py` | 格式解析 + token 级写回 |

层间职责边界（规范 §2）：

- 核心**不得**解析任何具体格式（核心 ↔ 适配器通过 §4.1 Adapter 接口 + §3 数据结构解耦）
- 适配器**不负责**文件 I/O / 编码探测 / 原子写 / 外部轮询（这些由核心统一处理）
- GUI / CLI 是薄壳——核心逻辑可独立单元测试

新增一种文件格式 = 写一个 `Adapter` 子类 + 注册，不动核心任何代码（§4.2）。

---

## ✦ 开发

```bash
# 安装（含 dev 依赖）
pip install -e ".[dev]"

# 跑测试
pytest

# 跑 GUI（开发模式）
configer open examples/*.yaml
```

**仓库布局**：

```
configer/
├── src/configer/          # 源码包
│   ├── __init__.py        # 版本号
│   ├── cli.py             # 命令行入口（规范 §9）
│   ├── model.py           # 语义模型（规范 §3）
│   ├── registry.py        # 适配器注册表（§4.2）
│   ├── bytesio.py         # 编码 / BOM / EOL 处理
│   ├── single_instance.py # 单实例 + 转交（§9.4）
│   ├── adapters/          # 格式适配器（§4）
│   ├── core/              # 核心引擎（§7 / §8）
│   │   ├── session.py     # 编辑生命周期 + 提交门控
│   │   ├── validation.py  # gate_edit
│   │   ├── writeback.py   # 原子写
│   │   ├── poller.py      # 外部变更轮询
│   │   └── checks.py      # 模型不变量（§3.9）
│   └── ui/                # GUI（nicegui）
├── docs/spec.md           # 规范 v0.4（自洽实现指南）
└── pyproject.toml
```

---

## ✦ 规范

完整设计见 [`docs/spec.md`](docs/spec.md)（v0.4）—— 自洽实现指南：

- §3 语义模型（ConfigDoc / ConfigItem / Diagnostic / EditOp）
- §4 适配器接口与注册
- §5 注释画像（YAML / Python）
- §6 结构化标注（@ 标注）
- §7 写回与保真规则
- §8 校验语义（硬 / 软校验）
- §9 CLI 契约
- §10 GUI 布局与交互
- §11 验收基准（A-1..A-10）
- §12 里程碑 M0–M3

---

## ✦ 状态

v0.4 草案——M0（规范定稿）+ M1（项目骨架与初始实现）已完成；后续里程碑见 §12。

---

## ✦ 致谢

- [ruamel.yaml](https://yaml.dev/) —— YAML round-trip
- [libcst](https://libcst.readthedocs.io/) —— Python CST 解析/编辑
- [NiceGUI](https://nicegui.io/) —— Python web GUI