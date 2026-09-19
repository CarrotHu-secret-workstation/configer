// =====================================================================
// configer 演示 — Pyodide 前端
// 在浏览器中真实运行 configer 引擎（model + adapters + validation）
// =====================================================================

const SAMPLES = {
  "yaml-robot": {
    format: "yaml",
    src: `# 机器人控制参数 v1

robot_mode: normal          # 运行模式 off | slow | normal | fast
debug: false

# 关节角度序列（YAML 列表 → 只读 container，Y-6）
joint_angles: [0.0, 0.5, 1.0, 1.5]

robot:
  ball_color: blue          # 球的颜色 red | blue | green | yellow
  speed_limit: 50           # 速度上限 米/秒
  ball_radius: 0.15         # 球半径 米
  enabled: on               # 总开关 on | off

vision:                     # 视觉模块
  enabled: on               # on | off
  exposure_ms: 8.0          # 曝光时间 毫秒
  threshold: 0.15           # 阈值 [0,1]

# 空配置块（空 mapping → 只读 container）
empty_block: {}
`,
  },

  "yaml-camera": {
    format: "yaml",
    src: `# 相机内参（容器只读示例）
camera:                     # 相机参数
  intrinsics:               # 内参（容器）
    fx: 525.0
    fy: 525.0
    cx: 320
    cy: 240
  resolution:               # 分辨率（容器）
    width: 640
    height: 480

# 镜头畸变系数（5 元素列表 → 只读）
distortion: [0.1, 0.05, 0.0, 0.0, 0.0]

# 可调参数
exposure_compensation: -0.5 # 曝光补偿 [-2, 2]
white_balance: 6500         # 白平衡 K
`,
  },

  "yaml-floats": {
    format: "yaml",
    src: `# 浮点字面量风格保留演示（§3.2）
# - 0.15 → plain
# - 100. → trailing_dot
# - 0.90 → fixed(2)
# - 1.0  → fixed(1)
pi_approx: 0.15
threshold: 100.
price: 0.90
ratio: 1.0
`,
  },

  "py-dronectrl": {
    format: "python",
    src: `"""droneCtrl 配置。

主旋翼角速度上限与机架尺寸。
"""

# ====================
# 控制参数
# ====================
# 本节总览: 控制器参数汇总

# 作用: 控制器模式选择
# 影响: 主循环分派
CTRL_MODE = "high"

# 作用: P 控制器增益
# 影响: 闭环响应速度
# 建议: [0, 100]
CTRL_P = 30

# ====================
# 机架
# ====================
# 本节总览: 物理参数

FRAME_SIZE = "medium"
FRAME_WEIGHT_G = 850

# ====================
# 电池
# ====================

BATTERY_CAPACITY_MAH = 5200
BATTERY_CUTOFF_V = 3.3
`,
  },

  "py-compute": {
    format: "python",
    src: `"""compute 模块 — 异体数字字面量示例。

含 0x / 0b / 下划线分隔 / 指数记法的数字 → v1 应只读（§3.2）。
"""

# 普通十进制（可编辑）
NORMAL_INT = 42
NORMAL_FLOAT = 0.5

# ====================
# 异体数字字面量（应只读）
# ====================

# 作用: 十六进制颜色
HEX_VALUE = 0xFF

# 作用: 二进制掩码
BIN_VALUE = 0b1010

# 作用: 千分位分隔
UNDERSCORE_VALUE = 1_000_000

# 作用: 科学记数
EXP_FORM = 1e3
`,
  },
};

// configer 引擎源码在 demo/lib/ 下，相对路径。GitHub Pages 会一并提供。
const LIB_FILES = [
  "lib/configer/__init__.py",
  "lib/configer/model.py",
  "lib/configer/bytesio.py",
  "lib/configer/registry.py",
  "lib/configer/adapters/__init__.py",
  "lib/configer/adapters/base.py",
  "lib/configer/adapters/yaml_adapter.py",
  "lib/configer/adapters/yaml_scalar.py",
  "lib/configer/adapters/python_adapter.py",
  "lib/configer/core/__init__.py",
  "lib/configer/core/validation.py",
  "lib/configer/core/checks.py",
];

// =====================================================================
// Pyodide 启动
// =====================================================================
let pyodide = null;
let engineReady = false;

const statusEl = document.getElementById("status");
const statusText = document.getElementById("status-text");
const setStatus = (text, kind = "loading") => {
  statusText.textContent = text;
  statusEl.classList.remove("ok", "err");
  if (kind === "ok") statusEl.classList.add("ok");
  if (kind === "err") statusEl.classList.add("err");
};

async function bootPyodide() {
  setStatus("加载 Pyodide 运行时 …");
  pyodide = await loadPyodide({
    indexURL: "https://cdn.jsdelivr.net/pyodide/v0.27.7/full/",
  });

  setStatus("安装 ruamel.yaml + libcst …");
  await pyodide.loadPackage(["micropip"]);
  const micropip = pyodide.pyimport("micropip");
  await micropip.install(["ruamel.yaml", "libcst"]);

  setStatus("载入 configer 引擎源码 …");
  // 把 demo/lib/configer/* 写到 Pyodide FS 的 /lib/configer/...
  for (const rel of LIB_FILES) {
    const pyPath = "/" + rel;
    const dir = pyPath.substring(0, pyPath.lastIndexOf("/"));
    if (dir && dir !== "/" && !pyodide.FS.analyzePath(dir).exists) {
      try {
        pyodide.FS.mkdirTree(dir);
      } catch {
        // 旧版 Pyodide 没有 mkdirTree，逐级 mkdir
        const parts = dir.split("/").filter(Boolean);
        let cur = "";
        for (const p of parts) {
          cur += "/" + p;
          if (!pyodide.FS.analyzePath(cur).exists) pyodide.FS.mkdir(cur);
        }
      }
    }
    const resp = await fetch(rel);
    if (!resp.ok) throw new Error(`无法获取 ${rel}: HTTP ${resp.status}`);
    const txt = await resp.text();
    pyodide.FS.writeFile(pyPath, txt);
  }

  setStatus("初始化引擎 …");
  pyodide.runPython(`
import sys, types, hashlib
sys.path.insert(0, "/lib")

# 共享状态：当前 doc + adapter
_state = types.SimpleNamespace(doc=None, adapter=None, path=None)

import configer
from configer.registry import default_registry
from configer.model import EditOp, Section, EnumCandidate, RangeConstraint, LiteralStyle
from configer.core.validation import gate_edit

REGISTRY = default_registry()
f"configer {configer.__version__} 已就绪，适配器：{[a.name for a in REGISTRY.adapters]}"
`);

  engineReady = true;
  setStatus("引擎就绪", "ok");
}

bootPyodide().catch((err) => {
  console.error(err);
  setStatus("启动失败：" + err.message, "err");
});

// =====================================================================
// UI 元素
// =====================================================================
const sourceEl = document.getElementById("source");
const byteCountEl = document.getElementById("byte-count");
const modelViewEl = document.getElementById("model-view");
const itemCountEl = document.getElementById("item-count");
const docFormatBadge = document.getElementById("doc-format");
const diagListEl = document.getElementById("diag-list");
const diagCountEl = document.getElementById("diag-count");
const editPathEl = document.getElementById("edit-path");
const editValueEl = document.getElementById("edit-value");
const editHintEl = document.getElementById("edit-hint");
const writebackPanel = document.getElementById("writeback-panel");
const writebackOutput = document.getElementById("writeback-output");
const writebackMeta = document.getElementById("writeback-meta");

function updateByteCount() {
  const n = new TextEncoder().encode(sourceEl.value).length;
  byteCountEl.textContent = `${n} 字节`;
}
sourceEl.addEventListener("input", updateByteCount);
updateByteCount();

function escapeHtml(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formatVal(v) {
  if (v === null || v === undefined) return "<i style='color:var(--text-dim)'>null</i>";
  if (typeof v === "string") return `"${escapeHtml(v)}"`;
  if (typeof v === "boolean") return v ? "true" : "false";
  return String(v);
}

// =====================================================================
// 示例选择 / 清空
// =====================================================================
const sampleSelect = document.getElementById("sample-select");
const formatSelect = document.getElementById("format-select");
const btnLoad = document.getElementById("btn-load");
const btnClear = document.getElementById("btn-clear");

sampleSelect.addEventListener("change", () => {
  const key = sampleSelect.value;
  if (!key) return;
  const sample = SAMPLES[key];
  if (!sample) return;
  sourceEl.value = sample.src;
  formatSelect.value = sample.format;
  updateByteCount();
  writebackPanel.hidden = true;
});

btnClear.addEventListener("click", () => {
  sourceEl.value = "";
  formatSelect.value = "auto";
  sampleSelect.value = "";
  updateByteCount();
  modelViewEl.innerHTML = '<p class="empty">已清空。</p>';
  itemCountEl.textContent = "0 项 / 0 组";
  docFormatBadge.textContent = "—";
  docFormatBadge.className = "badge";
  diagListEl.innerHTML = '<li class="empty">无</li>';
  diagCountEl.textContent = "0 条";
  editPathEl.innerHTML = "<option value=\"\">— 选择条目 —</option>";
  editValueEl.value = "";
  editHintEl.textContent = "";
  writebackPanel.hidden = true;
  pyodide?.runPython?.("_state.doc = None; _state.adapter = None; _state.path = None");
});

// =====================================================================
// 加载：源码 → 语义模型
// =====================================================================
btnLoad.addEventListener("click", async () => {
  if (!engineReady) {
    alert("Pyodide 引擎尚未就绪，请稍候 …");
    return;
  }
  const text = sourceEl.value;
  if (!text.trim()) {
    alert("源码不能为空");
    return;
  }
  const fmt = formatSelect.value;
  try {
    await loadAndRender(text, fmt);
  } catch (err) {
    console.error(err);
    setStatus("加载失败：" + (err.message || err), "err");
    alert("加载失败：" + (err.message || err));
  }
});

async function loadAndRender(text, fmt) {
  setStatus("解析中 …");
  // 决定文件扩展名（用于嗅探）：auto 模式给 .yaml 兜底
  const ext = fmt === "python" ? ".py" : fmt === "yaml" ? ".yaml" : ".yaml";
  const filename = "/tmp/demo_config" + ext;
  pyodide.FS.writeFile(filename, new TextEncoder().encode(text));

  // 调引擎：选定适配器 + 加载
  const py = `
import json
from pathlib import Path

src_path = Path(${JSON.stringify(filename)})
head = src_path.read_bytes()[:64]

adapter, diags = REGISTRY.select_adapter(src_path, head, format_hint=${JSON.stringify(fmt)})
if adapter is None:
    errs = [{"severity": d.severity, "code": d.code, "message": d.message, "path": d.path} for d in diags]
    raise SystemExit(json.dumps({"error": "format", "diagnostics": errs}, ensure_ascii=False))

doc, load_diags = adapter.load(src_path)
doc.refresh_hash()

_state.doc = doc
_state.adapter = adapter
_state.path = src_path

items = []
for it in doc.items:
    items.append({
        "path": it.path,
        "group": it.group,
        "subgroup": it.subgroup,
        "type": it.type,
        "value": it.value,
        "raw_literal": it.raw_literal,
        "readonly": it.readonly,
        "readonly_reason": it.readonly_reason,
        "dormant": it.dormant,
        "warning": it.warning,
        "description": [{"label": s.label, "text": s.text} for s in it.description],
        "enum_candidates": [{"value": c.value, "label": c.label, "provenance": c.provenance} for c in it.enum_candidates],
        "range": ({"min": it.range.min, "max": it.range.max, "unit": it.range.unit} if it.range else None),
    })

groups = [{"name": g.name, "description": g.description} for g in doc.groups]
all_diags = [{"severity": d.severity, "code": d.code, "message": d.message, "path": d.path} for d in (list(doc.diagnostics) + list(load_diags))]
result = {
    "format": doc.format,
    "items": items,
    "groups": groups,
    "diagnostics": all_diags,
}
json.dumps(result, ensure_ascii=False, default=str)
`;
  const jsonText = pyodide.runPython(py);
  const data = JSON.parse(jsonText);

  renderModel(data);
  renderDiagnostics(data.diagnostics);
  populateEditSelect(data.items);

  setStatus(`已解析 ${data.items.length} 项（${data.format}）`, "ok");
}

function renderModel(data) {
  docFormatBadge.textContent = data.format;
  docFormatBadge.className = `badge ${data.format}`;

  const byGroup = new Map();
  for (const it of data.items) {
    const g = it.group || "（根级）";
    if (!byGroup.has(g)) byGroup.set(g, []);
    byGroup.get(g).push(it);
  }
  itemCountEl.textContent = `${data.items.length} 项 / ${byGroup.size} 组`;

  let html = "";
  const orderedGroups = [];
  for (const g of data.groups) {
    if (byGroup.has(g.name)) {
      orderedGroups.push([g, byGroup.get(g.name)]);
      byGroup.delete(g.name);
    }
  }
  for (const [gname, items] of byGroup) {
    orderedGroups.push([{ name: gname, description: null }, items]);
  }

  for (const [g, items] of orderedGroups) {
    html += `<div class="group-block">`;
    html += `<div class="group-head"><span>${escapeHtml(g.name)}</span><span class="count">${items.length}</span></div>`;
    if (g.description) {
      html += `<div class="item-desc" style="padding:6px 10px;border-bottom:1px solid var(--line);">${escapeHtml(g.description)}</div>`;
    }
    for (const it of items) html += renderItem(it);
    html += `</div>`;
  }
  if (data.items.length === 0) {
    html = '<p class="empty">解析成功但未提取到任何条目。</p>';
  }
  modelViewEl.innerHTML = html;
}

function renderItem(it) {
  const flags = [];
  if (it.readonly) flags.push(`<span class="flag readonly" title="${escapeHtml(it.readonly_reason || "")}">readonly${it.readonly_reason ? ":" + it.readonly_reason : ""}</span>`);
  if (it.dormant)  flags.push(`<span class="flag dormant">dormant</span>`);
  if (it.warning)  flags.push(`<span class="flag warning">warning</span>`);

  let desc = "";
  if (it.description && it.description.length) {
    desc = `<div class="item-desc">`;
    for (const s of it.description) {
      if (s.label) desc += `<b>${escapeHtml(s.label)}：</b> `;
      desc += escapeHtml(s.text) + "<br>";
    }
    desc += `</div>`;
  }

  let constraints = "";
  if (it.enum_candidates && it.enum_candidates.length) {
    const opts = it.enum_candidates
      .map(c => `${formatVal(c.value)}${c.provenance === "inferred" ? "*" : ""}`)
      .join(" · ");
    constraints += `<span class="enum">枚举：${escapeHtml(opts)}</span>`;
  }
  if (it.range) {
    const r = it.range;
    const parts = [];
    if (r.min !== null) parts.push(`≥ ${r.min}`);
    if (r.max !== null) parts.push(`≤ ${r.max}`);
    if (parts.length) {
      constraints += `<span class="range">范围：${escapeHtml(parts.join("  "))}${r.unit ? " " + escapeHtml(r.unit) : ""}</span>`;
    }
  }
  const constraintBlock = constraints ? `<div class="item-constraints">${constraints}</div>` : "";

  return `
    <div class="item">
      <div class="item-head">
        <span class="item-path">${escapeHtml(it.path)}</span>
        <span class="item-type ${it.type || "none"}">${it.type || "?"}</span>
        <div class="item-flags">${flags.join("")}</div>
      </div>
      <div class="item-value">
        <b>${formatVal(it.value)}</b>
        <span class="raw">${escapeHtml(it.raw_literal)}</span>
      </div>
      ${desc}
      ${constraintBlock}
    </div>
  `;
}

function renderDiagnostics(diags) {
  diagCountEl.textContent = `${diags.length} 条`;
  if (!diags.length) {
    diagListEl.innerHTML = '<li class="empty">无</li>';
    return;
  }
  diagListEl.innerHTML = diags.map(d => `
    <li class="${d.severity}">
      <span class="code">${escapeHtml(d.code)}</span>
      ${escapeHtml(d.message)}
      ${d.path ? `<span class="path">${escapeHtml(d.path)}</span>` : ""}
    </li>
  `).join("");
}

function populateEditSelect(items) {
  editPathEl.innerHTML = '<option value="">— 选择条目 —</option>' +
    items
      .filter(it => !it.readonly)
      .map(it => {
        const v = it.value === null ? "null" : String(it.value);
        return `<option value="${escapeHtml(it.path)}" data-type="${it.type || ""}" data-value="${escapeHtml(v)}">${escapeHtml(it.path)}  (${it.type || "?"} = ${escapeHtml(v)})</option>`;
      })
      .join("");
  editPathEl.onchange = () => {
    const opt = editPathEl.selectedOptions[0];
    if (opt && opt.dataset.type) {
      editValueEl.placeholder = `按 type (${opt.dataset.type}) 输入新值`;
    }
  };
}

// =====================================================================
// 应用编辑：EditOp → 写回字节
// =====================================================================
document.getElementById("btn-apply").addEventListener("click", async () => {
  const path = editPathEl.value;
  const newValueRaw = editValueEl.value;
  if (!path) { alert("请选择条目"); return; }
  if (newValueRaw === "") { alert("请输入新值"); return; }
  try {
    await applyEdit(path, newValueRaw);
  } catch (err) {
    console.error(err);
    editHintEl.textContent = "失败：" + (err.message || err);
    editHintEl.style.color = "var(--err)";
  }
});

async function applyEdit(path, rawValue) {
  setStatus("应用编辑 …");
  const opt = Array.from(editPathEl.options).find(o => o.value === path);
  const type = opt?.dataset.type;
  const py = `
import json
from configer.model import EditOp
from configer.core.validation import gate_edit

path = ${JSON.stringify(path)}
type_str = ${JSON.stringify(type)}
raw = ${JSON.stringify(rawValue)}

# 按 type 解析新值
if type_str == "bool":
    if raw.lower() in ("true", "1", "on", "yes"):
        nv = True
    elif raw.lower() in ("false", "0", "off", "no"):
        nv = False
    else:
        raise ValueError(f"无法解析 bool: {raw!r}")
elif type_str == "int":
    nv = int(raw)
elif type_str == "float":
    nv = float(raw)
elif type_str == "str":
    nv = raw
else:
    nv = raw

# 找到条目
item = next((it for it in _state.doc.items if it.path == path), None)
if item is None:
    raise ValueError(f"未找到条目: {path}")
if item.readonly:
    raise ValueError(f"条目 {path} 为只读（{item.readonly_reason}）")

# 提交门控（§7.5 第 2 步、§8.2）
gate = gate_edit(item, nv)
if gate.level == "block":
    raise ValueError(f"门控拦截（{gate.provenance or ''} {gate.reason or ''}）")

edits = [EditOp(path=path, new_value=gate.value)]
new_bytes = _state.adapter.save(_state.doc, edits)
text = new_bytes.decode("utf-8", errors="replace")
sha = hashlib.sha256(new_bytes).hexdigest()[:16]

# 按规范 §7.5：save 后须 reload 才能继续编辑
_state.doc = _state.adapter.load(_state.path)[0]
_state.doc.refresh_hash()

json.dumps({
    "ok": True,
    "text": text,
    "sha": sha,
    "new_value": gate.value,
    "gate_level": gate.level,
    "gate_reason": gate.reason,
    "gate_provenance": gate.provenance,
}, ensure_ascii=False)
`;
  const out = JSON.parse(pyodide.runPython(py));

  writebackPanel.hidden = false;
  writebackOutput.textContent = out.text;
  const gateInfo = out.gate_level === "warn"
    ? ` · ⚠ 警告（${out.gate_reason}）`
    : "";
  writebackMeta.textContent = `${out.text.length} 字节  ·  sha256:${out.sha}  ·  new_value=${JSON.stringify(out.new_value)}${gateInfo}`;

  if (out.gate_level === "warn") {
    editHintEl.textContent = `⚠ 已提交（推测警告：${out.gate_reason}）`;
    editHintEl.style.color = "var(--warn)";
  } else {
    editHintEl.textContent = "✓ 已提交（基线已 reload）";
    editHintEl.style.color = "var(--ok)";
  }

  setStatus("编辑已落盘（演示）", "ok");
}