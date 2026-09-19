"""设计令牌 + 全局样式（Apple-HIG 风格视觉重设计 Round 1）。

本模块是**视觉决策的唯一来源**：颜色 / 字号 / 间距 / 圆角 / 阴影 / 动效全部
以 CSS 自定义属性（:root 令牌）表达；layout.py 不出现一次性十六进制色值或
魔法像素值。深色模式由 ``@media (prefers-color-scheme: dark)`` 驱动：
``ui.dark_mode(None)`` 把明暗交给浏览器（Quasar 的 body--dark 亦跟随同一媒体
特性），本表为两种方案给出完整落地色——包括 Quasar 组件表面覆盖。html 上
用 CSS 声明 ``color-scheme: light dark``（CSS 属性优先于 NiceGUI 注入的
color-scheme meta，故浏览器不再对该页做强制暗色反色）。

**保留的既有钩子（测试/其它模块钉死，勿改名）**：``.gate-error`` /
``.gate-warn``（commit.marker_class 返回值，commit.py 不许改）、``mark``
（搜索命中高亮）、``.sidebar-collapsed``（收起侧栏时隐藏分割条，U-1）。

公开 API（Round 2 面板代理照此使用；不要再引入一次性样式）
----------------------------------------------------------

- :func:`install`：build_gui 首次构建时调用一次——注入 :data:`CSS` 与
  :data:`HEAD_HTML`（lang=zh-CN / color-scheme meta），并把明暗交给浏览器。
- :data:`CSS` / :data:`HEAD_HTML`：样式表与 head 片段（一般无需直接用）。
- :func:`space` / :func:`pad` / :func:`gap`：需要内联 ``style()`` 时取令牌
  字符串，例 ``.style(theme.pad(3, 4) + "; " + theme.gap(2))``。

CSS 类契约（全部由令牌实现）：

类型角色
  ``.t-title``（15px/600）· ``.t-body``（14px，行高 1.55）·
  ``.t-label``（12.5px/500）· ``.t-meta``（12px）·
  ``.t-mono``（13px，ui-monospace 栈）·
  ``.icon-sm``（16px）/ ``.icon-lg``（24px）图标尺寸

文字颜色
  ``.fg-primary`` · ``.fg-secondary`` · ``.fg-tertiary`` · ``.fg-accent`` ·
  ``.fg-error`` · ``.fg-warn`` · ``.fg-success``

表面与容器
  ``.pane`` 内容面板（白底 + 发丝线 + radius-md + 极浅阴影，**不含内边距**）
  ``.pane-col`` / ``.pane-row`` 面板内 flex 列/行（min-height/width: 0 防裁切）
  ``.pane-scroll`` 面板内滚动（overflow: auto，overscroll 不外溢）
  ``.pane-pad`` / ``.pane-pad-sm`` 面板内边距（space-3 / space-2）
  ``.surface-app`` 应用底色 · ``.surface-sunken`` 下沉底

状态与反馈
  ``.feedback-chip`` + ``.chip-error`` / ``.chip-warn`` / ``.chip-info`` /
  ``.chip-success``（工具栏回显/错误条）
  ``.banner`` + ``.banner-error`` / ``.banner-warn`` / ``.banner-info``
  （横幅条）；两者均“底色 + 边框 + 图标 + 文字”四重信号，颜色只是强化
  ``.gate-error`` / ``.gate-warn`` 门控红/黄标（commit.marker_class 钩子）
  ``.empty-state`` / ``.empty-state-icon`` 空态

壳层与控件（layout.py 专用）
  ``.app-shell`` · ``.app-toolbar`` · ``.app-toolbar-group`` ·
  ``.app-toolbar-feedback`` · ``.app-toolbar-spacer`` · ``.app-exit`` ·
  ``.app-banner-strip`` · ``.app-main`` · ``.main-region`` · ``.icon-btn`` ·
  ``.file-header`` · ``.file-doc`` · ``.echo-text``（回显上限宽度令牌）

Round 2 追加（面板/对话框复用，语义不变）
  ``.list-row`` / ``.list-row-active`` / ``.list-row-name`` /
  ``.list-row-quiet`` 列表行（侧栏文件与条目列表共用同一行语言）；
  ``.focusable-row`` 可聚焦行（roving tabindex + 令牌焦点环，S8）；
  ``.badge`` + ``.badge-warn`` / ``.badge-error`` / ``.badge-inferred``
  状态徽标（文字始终可见，颜色只是强化；kind → 类映射见 :data:`BADGE_TONE`）；
  ``.pane-head`` / ``.pane-foot`` / ``.pane-title-row`` 面板头/脚；
  ``.detail-section`` 详情分区（+ 分隔线）· ``.code-box`` 只读值/依据原文
  展示盒；``.dialog-card`` / ``.dialog-head`` / ``.dialog-list`` /
  ``.dialog-actions`` 四个对话框统一骨架。
  ``.gate-error`` / ``.gate-warn`` 钩子类名不变：R2 起不再给容器画第二层
  框，改为给内部输入控件染边（原因行承担图标 + 文字）。

动效：仅 hover/选中/浮层进入，120–160ms；``prefers-reduced-motion: reduce``
下全部关闭。窄屏（≤760px / ≤480px）只收紧间距令牌，不删减内容。
"""

from __future__ import annotations

from nicegui import ui

__all__ = ["CSS", "HEAD_HTML", "BADGE_TONE", "install", "space", "pad", "gap"]

# 条目状态徽标（``logic.badge_kinds`` 的 kind）→ 主题徽标类（items / detail
# 共用，取代两份重复的 BADGE_COLOR 字典）。徽标**文字**来自 logic.BADGE_TEXT
# （只读/休眠/警告/推测范围/推测候选），颜色与虚线只是强化信号。
BADGE_TONE: dict[str, str] = {
    "readonly": "badge",
    "dormant": "badge badge-warn",
    "warning": "badge badge-warn",
    "inferred_range": "badge badge-inferred",
    "inferred_enum": "badge badge-inferred",
}


# ---------------------------------------------------------------------------
# head 片段：语言 + color-scheme
# ---------------------------------------------------------------------------
# NiceGUI 模板先注入 ``<meta id="nicegui-color-scheme" content="normal">``
# （ui.dark_mode(None) 的 auto 语义），head_html 排在其后；两者都是“交给
# 浏览器”的中性声明。真正钉死明暗支持的是 CSS 的 ``color-scheme: light dark``
# （CSS 属性优先于 meta），浏览器据此不再做强制暗色反色（旧缺陷根因）。
HEAD_HTML = """
<meta name="color-scheme" content="light dark">
<script>document.documentElement.lang = "zh-CN";</script>
"""


# ---------------------------------------------------------------------------
# 样式表
# ---------------------------------------------------------------------------

CSS = """
/* ==========================================================================
   1. 令牌：间距 / 圆角 / 字体 / 颜色 / 阴影 / 动效
   ========================================================================== */
:root {
  color-scheme: light dark;

  /* 间距 4 / 8 / 12 / 16 / 24 / 32 */
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 24px;
  --space-6: 32px;

  /* 圆角 */
  --radius-sm: 6px;
  --radius-md: 10px;
  --radius-lg: 14px;
  --radius-pill: 999px;

  /* 字体 */
  --font-sans: -apple-system, BlinkMacSystemFont, "SF Pro Text",
    "Helvetica Neue", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei",
    "Noto Sans SC", sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
    "Liberation Mono", "Courier New", monospace;
  --fs-title: 15px;   --fw-title: 600;  --lh-title: 1.35;
  --fs-body: 14px;                       --lh-body: 1.55;
  --fs-label: 12.5px; --fw-label: 500;  --lh-label: 1.45;
  --fs-meta: 12px;                       --lh-meta: 1.45;
  --fs-mono: 13px;

  /* 表面 */
  --surface-app: #f5f5f7;
  --surface-content: #ffffff;
  --surface-overlay: #ffffff;
  --surface-tint: rgba(0, 0, 0, 0.04);
  --surface-sunken: rgba(0, 0, 0, 0.028);
  --surface-toolbar: rgba(255, 255, 255, 0.78);
  --hairline: rgba(0, 0, 0, 0.08);
  --hairline-strong: rgba(0, 0, 0, 0.16);

  /* 文字（S3：tertiary 也须达正文 AA 4.5:1——原 #86868b 白底 3.62:1；
     secondary 随之下移一档保持三级层次） */
  --text-primary: #1d1d1f;
  --text-secondary: #565659;
  --text-tertiary: #6e6e73;
  --text-on-accent: #ffffff;

  /* 强调色（分职：--accent 作实心填充，白字 4.70:1；--accent-text 作
     文字/图标，在 accent-soft 软底上 4.85:1） */
  --accent: #0071e3;
  --accent-hover: #0077ed;
  --accent-text: #0066cc;
  --accent-soft: rgba(0, 113, 227, 0.10);
  --accent-ring: rgba(0, 113, 227, 0.30);

  /* 状态色（各带柔和底；文字色按正文 AA 4.5:1 选值——原 warn 3.07:1 /
     error 4.41:1 / success 4.25:1 / info 4.22:1，S4；色相族不变） */
  --status-error: #c81e1e;
  --status-error-bg: #fef2f2;
  --status-error-border: rgba(200, 30, 30, 0.30);
  --status-warn: #b45309;
  --status-warn-bg: #fffbeb;
  --status-warn-border: rgba(180, 83, 9, 0.32);
  --status-success: #0b7a34;
  --status-success-bg: #f0fdf4;
  --status-success-border: rgba(11, 122, 52, 0.28);
  --status-info: #0066cc;
  --status-info-bg: rgba(0, 102, 204, 0.08);
  --status-info-border: rgba(0, 102, 204, 0.26);
  --mark-bg: #fde68a;

  /* 阴影（仅两级：卡片 / 浮层） */
  --shadow-card: 0 1px 2px rgba(0, 0, 0, 0.05), 0 1px 1px rgba(0, 0, 0, 0.03);
  --shadow-overlay: 0 12px 32px rgba(0, 0, 0, 0.18),
    0 2px 8px rgba(0, 0, 0, 0.10);

  /* 其它 */
  --ring-focus: 0 0 0 3px var(--accent-ring);
  --motion-fast: 140ms;
  --motion-base: 160ms;
  --ease-standard: cubic-bezier(0.22, 0.61, 0.36, 1);
  --toolbar-height: 48px;
  --toolbar-blur: saturate(180%) blur(20px);
  --echo-max: 24rem;                /* 工具栏"最近提交回显"上限宽度 */
}

/* 深色令牌：与 ui.dark_mode(None) 跟随同一媒体特性，Quasar 的 body--dark
   落在同一分支；本表为非分层样式，覆盖顺序上恒胜过 Quasar 分层样式。 */
@media (prefers-color-scheme: dark) {
  :root {
    --surface-app: #1c1c1e;
    --surface-content: #242426;
    --surface-overlay: #2c2c2e;
    --surface-tint: rgba(255, 255, 255, 0.06);
    --surface-sunken: rgba(255, 255, 255, 0.035);
    --surface-toolbar: rgba(28, 28, 30, 0.72);
    --hairline: rgba(255, 255, 255, 0.12);
    --hairline-strong: rgba(255, 255, 255, 0.24);

    --text-primary: #f5f5f7;
    --text-secondary: #a1a1a6;
    --text-tertiary: #949499;   /* 下沉底（只读原文 code-box）上 4.63:1 */

    /* 强调色分职（S5）：--accent 实心填充（白字 4.91:1，原 #0a84ff 3.65:1）；
       --accent-text 深色面上的文字/图标（内容面 5.90:1，原 #0a84ff 4.23:1） */
    --accent: #0a6edc;
    --accent-hover: #409cff;
    --accent-text: #4da3ff;
    --accent-soft: rgba(10, 110, 220, 0.16);
    --accent-ring: rgba(10, 110, 220, 0.45);

    /* 状态色（S5：文字提亮——原 error 3.88:1 / info 3.63:1 不足 4.5:1；
       warn/success 已在 4.5:1 以上，保持原值） */
    --status-error: #ff6b60;
    --status-error-bg: rgba(255, 107, 96, 0.14);
    --status-error-border: rgba(255, 107, 96, 0.36);
    --status-warn: #ff9f0a;
    --status-warn-bg: rgba(255, 159, 10, 0.14);
    --status-warn-border: rgba(255, 159, 10, 0.36);
    --status-success: #30d158;
    --status-success-bg: rgba(48, 209, 88, 0.14);
    --status-success-border: rgba(48, 209, 88, 0.32);
    --status-info: #4da3ff;
    --status-info-bg: rgba(77, 163, 255, 0.14);
    --status-info-border: rgba(77, 163, 255, 0.36);
    --mark-bg: rgba(255, 214, 10, 0.32);

    --shadow-card: 0 1px 2px rgba(0, 0, 0, 0.45);
    --shadow-overlay: 0 16px 40px rgba(0, 0, 0, 0.55),
      0 2px 10px rgba(0, 0, 0, 0.40);
  }
}

/* ==========================================================================
   2. 基础：文档、NiceGUI 内容根、Quasar 品牌色重定向
   ========================================================================== */
html, body { height: 100%; }

body {
  margin: 0;
  overflow: hidden;                 /* 滚动由 .pane-scroll 承担（整屏 IDE 壳层） */
  background-color: var(--surface-app);
  color: var(--text-primary);
  font-family: var(--font-sans);
  font-size: var(--fs-body);
  line-height: var(--lh-body);
  -webkit-font-smoothing: antialiased;
  /* NiceGUI 在 body 内联 --q-*（quasar_config.brand 缺省值）：inline 样式
     需 !important 才能让 Quasar 组件（开关/下拉/徽标/通知）跟随本表令牌 */
  --q-primary: var(--accent) !important;
  --q-negative: var(--status-error) !important;
  --q-warning: var(--status-warn) !important;
  --q-positive: var(--status-success) !important;
  --q-info: var(--status-info) !important;
}

.nicegui-content {
  padding: 0;
  gap: 0;
  width: 100%;
}

/* ==========================================================================
   3. 类型角色与文字颜色
   ========================================================================== */
.t-title {
  font-size: var(--fs-title);
  font-weight: var(--fw-title);
  line-height: var(--lh-title);
  letter-spacing: -0.01em;
}
.t-body { font-size: var(--fs-body); line-height: var(--lh-body); }
.t-label {
  font-size: var(--fs-label);
  font-weight: var(--fw-label);
  line-height: var(--lh-label);
}
.t-meta { font-size: var(--fs-meta); line-height: var(--lh-meta); }
.t-mono { font-family: var(--font-mono); font-size: var(--fs-mono); }

.fg-primary { color: var(--text-primary); }
.fg-secondary { color: var(--text-secondary); }
.fg-tertiary { color: var(--text-tertiary); }
.fg-accent { color: var(--accent-text); }
.fg-error { color: var(--status-error); }
.fg-warn { color: var(--status-warn); }
.fg-success { color: var(--status-success); }

/* 搜索命中高亮（S6）：底色之外文字固定为主文字色——否则安静/选中行
   继承的弱色/强调色落在黄底上仅 1.80–2.91:1。 */
mark {
  background: var(--mark-bg);
  color: var(--text-primary);
  border-radius: 2px;
  padding: 0 2px;
}

/* ==========================================================================
   4. 表面 / 面板
   ========================================================================== */
.surface-app { background-color: var(--surface-app); }
.surface-sunken { background-color: var(--surface-sunken); }

.pane {
  background-color: var(--surface-content);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-card);
  min-width: 0;
  min-height: 0;
}
.pane-col { display: flex; flex-direction: column; }
.pane-row { display: flex; flex-direction: row; }
.pane-scroll { overflow: auto; overscroll-behavior: contain; }
.pane-pad { padding: var(--space-3); }
.pane-pad-sm { padding: var(--space-2); }

/* ==========================================================================
   5. 壳层：满高 flex 列（100dvh，100vh 回退）+ 唯一半透明工具栏
   ========================================================================== */
.app-shell {
  display: flex;
  flex-direction: column;
  align-self: stretch;
  width: 100%;
  height: 100vh;
  height: 100dvh;                 /* 动态视口高度：移动端地址栏不裁切 */
  min-height: 0;
  background-color: var(--surface-app);
  color: var(--text-primary);
}

.app-toolbar {
  position: sticky;               /* 内容面板各自滚动，工具栏恒在顶部 */
  top: 0;
  z-index: 20;
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-height: var(--toolbar-height);
  padding: 0 var(--space-3);
  background-color: var(--surface-toolbar);
  -webkit-backdrop-filter: var(--toolbar-blur);
  backdrop-filter: var(--toolbar-blur);
  border-bottom: 1px solid var(--hairline);
}
.app-toolbar-group {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  flex: 0 0 auto;
}
.app-toolbar-feedback {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex: 0 1 auto;
  min-width: 0;
}
.app-toolbar-feedback > * { min-width: 0; }
.app-toolbar-spacer { flex: 1 1 auto; min-width: var(--space-2); }
/* 最近提交回显（U-7）：上限宽度走令牌（长值不挤占工具栏，仍截断） */
.echo-text { max-width: var(--echo-max); }

.app-toolbar .q-btn {
  color: var(--text-secondary);
  border-radius: var(--radius-pill);
  transition: color var(--motion-fast) var(--ease-standard),
    background-color var(--motion-fast) var(--ease-standard);
}
.app-toolbar .q-btn:hover { color: var(--text-primary); }
.app-toolbar .q-btn:focus-visible { outline: none; box-shadow: var(--ring-focus); }
.app-toolbar .q-btn.disabled { color: var(--text-tertiary); }

/* 退出为破坏性动作：静默次级样式，仅 hover/focus 转红 */
.app-toolbar .app-exit { color: var(--text-tertiary); }
.app-toolbar .app-exit:hover,
.app-toolbar .app-exit:focus-visible {
  color: var(--status-error);
  background-color: var(--status-error-bg);
}

.icon-btn { min-width: 34px; min-height: 34px; }

/* 横幅条：贴工具栏下方，无横幅 ⇒ 零高。条内恒有 refreshable 包装元素，
   ``:not(:empty)`` 永不匹配（N1，8px 永久留白）；改按实际横幅行判定。 */
.app-banner-strip {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: 0 var(--space-3);
}
.app-banner-strip:has(.banner) { padding-top: var(--space-2); }

/* 主区：应用底色 + 统一内边距，面板作为内容表面浮于其上 */
.app-main {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  padding: var(--space-2) var(--space-3) var(--space-3);
}
.main-region {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  min-width: 0;
  min-height: 0;
}
.main-region > .q-splitter { flex: 1 1 auto; min-height: 0; }
.app-main > .q-splitter { flex: 1 1 auto; min-height: 0; }   /* 外层（侧栏|主区） */

/* 分割条：默认隐去 1px 灰线，改为面板间距 + hover/拖拽高亮 */
.q-splitter__separator {
  background-color: transparent;
  transition: background-color var(--motion-fast) var(--ease-standard);
}
.q-splitter--vertical > .q-splitter__separator { width: var(--space-2); }
.q-splitter__separator:hover,
.q-splitter--active > .q-splitter__separator { background-color: var(--accent); }
.q-splitter__panel { min-width: 0; min-height: 0; }
/* 收起侧栏（U-1 钩子，勿改名）：隐藏分割条并禁用其命中区 */
.sidebar-collapsed > .q-splitter__separator {
  visibility: hidden;
  pointer-events: none;
}

/* ==========================================================================
   6. 反馈层：横幅 / 错误条 / 空态 / 门控标记
   ========================================================================== */
.banner {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md);
  background-color: var(--surface-content);
  color: var(--text-primary);
  font-size: var(--fs-label);
  font-weight: var(--fw-label);
  line-height: var(--lh-label);
  box-shadow: var(--shadow-card);
}
.banner > .q-icon { flex: 0 0 auto; }
.banner-error {
  border-color: var(--status-error-border);
  background-color: var(--status-error-bg);
  color: var(--status-error);
}
.banner-warn {
  border-color: var(--status-warn-border);
  background-color: var(--status-warn-bg);
  color: var(--status-warn);
}
.banner-info {
  border-color: var(--status-info-border);
  background-color: var(--status-info-bg);
  color: var(--status-info);
}
.banner .q-btn { color: inherit; }

.feedback-chip {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  min-width: 0;
  padding: var(--space-1) var(--space-2);
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  font-size: var(--fs-label);
  line-height: var(--lh-label);
}
.feedback-chip > .q-icon { flex: 0 0 auto; }
.feedback-chip .q-btn { color: inherit; }
.feedback-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chip-error {
  border-color: var(--status-error-border);
  background-color: var(--status-error-bg);
  color: var(--status-error);
}
.chip-warn {
  border-color: var(--status-warn-border);
  background-color: var(--status-warn-bg);
  color: var(--status-warn);
}
.chip-info {
  border-color: var(--status-info-border);
  background-color: var(--status-info-bg);
  color: var(--status-info);
}
.chip-success {
  border-color: var(--status-success-border);
  background-color: var(--status-success-bg);
  color: var(--status-success);
}

/* 空态（文案在 layout.py，此处只负责居中与节奏） */
.empty-state {
  display: flex;
  flex: 1 1 auto;
  min-height: 0;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  padding: var(--space-6) var(--space-4);
  text-align: center;
}
.empty-state-icon { font-size: 40px; color: var(--text-tertiary); }
/* 图标尺寸（替代 Tailwind text-base / text-2xl 一次性类） */
.icon-sm { font-size: 16px; }
.icon-lg { font-size: 24px; }

/* 空态插槽（N2：layout 的空态容器类）：撑满主区剩余高度，内部
   .empty-state 才能纵向居中（此前未定义 → 空态顶到上边）。 */
.empty-slot {
  display: flex;
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
}

/* 门控红/黄标（commit.marker_class 钩子，勿改名；颜色只是强化，
   原因文字由 detail 面板就地展示） */
.gate-error {
  border: 1px solid var(--status-error);
  background: var(--status-error-bg);
  border-radius: var(--radius-sm);
}
.gate-warn {
  border: 1px solid var(--status-warn);
  background: var(--status-warn-bg);
  border-radius: var(--radius-sm);
}

/* 主区文件头（文件名 / 格式徽标 / 文档级说明 / 诊断入口） */
.file-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex: 0 0 auto;
  flex-wrap: wrap;
  row-gap: var(--space-1);
  min-width: 0;
  padding: var(--space-2) var(--space-3);
  background-color: var(--surface-content);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-card);
}
.file-doc { flex: 0 0 auto; }

/* ==========================================================================
   7. Quasar 组件表面（两种配色方案下都跟令牌走）
   ========================================================================== */
/* 输入 / 下拉（outlined） */
.q-field--outlined .q-field__control {
  background-color: var(--surface-content);
  border-radius: var(--radius-sm);
}
.q-field--outlined .q-field__control:before {
  border: 1px solid var(--hairline-strong);
}
.q-field--outlined.q-field--focused .q-field__control:after {
  border: 2px solid var(--accent);
  border-radius: var(--radius-sm);
}
.q-field__native,
.q-field__input { color: var(--text-primary); }
.q-field__native::placeholder { color: var(--text-tertiary); }
.q-field__label { color: var(--text-secondary); }
.q-field__bottom { color: var(--text-tertiary); }
.q-menu .q-item,
.q-item__label { color: var(--text-primary); }
.q-menu .q-item:hover,
.q-item--clickable:hover { background-color: var(--surface-tint); }

/* 徽标 */
.q-badge {
  border-radius: var(--radius-sm);
  font-weight: var(--fw-label);
  letter-spacing: 0;
}

/* 折叠项（条目树组头） */
.q-expansion-item .q-item {
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  transition: background-color var(--motion-fast) var(--ease-standard);
}
.q-expansion-item .q-item:hover { background-color: var(--surface-tint); }
.q-expansion-item__toggle-icon { color: var(--text-tertiary); }

/* 对话框 / 菜单 / 提示 */
.q-dialog .q-card {
  background-color: var(--surface-overlay);
  color: var(--text-primary);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-overlay);
  max-width: calc(100vw - 2 * var(--space-4));   /* 窄屏不横向溢出 */
}
.q-menu {
  background-color: var(--surface-overlay);
  color: var(--text-primary);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-overlay);
}
.q-tooltip {
  background-color: var(--surface-overlay);
  color: var(--text-primary);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  box-shadow: var(--shadow-overlay);
  font-size: var(--fs-meta);
}
/* 通知（toast）：不沿用 Quasar 的类型实心底 + 强制白字（S5：深色亮色
   底上正向 2.02:1）；类型色只染底与文字，柔和底 + 状态文字与横幅/错误条
   同一语言。底色 = 状态色 6% 混入浮层表面（两种方案均 ≥4.5:1）。 */
.q-notification {
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-overlay);
  font-size: var(--fs-label);
  background-color: var(--surface-overlay) !important;  /* 未带类型时回落 */
  color: var(--text-primary) !important;                /* 压过 .text-white */
  border: 1px solid var(--hairline);
}
.q-notification.bg-primary {
  background-color: color-mix(in srgb, var(--accent) 6%, var(--surface-overlay)) !important;
  color: var(--accent-text) !important;
  border-color: var(--accent-ring);
}
.q-notification.bg-positive {
  background-color: color-mix(in srgb, var(--status-success) 6%, var(--surface-overlay)) !important;
  color: var(--status-success) !important;
  border-color: var(--status-success-border);
}
.q-notification.bg-negative {
  background-color: color-mix(in srgb, var(--status-error) 6%, var(--surface-overlay)) !important;
  color: var(--status-error) !important;
  border-color: var(--status-error-border);
}
.q-notification.bg-warning {
  background-color: color-mix(in srgb, var(--status-warn) 6%, var(--surface-overlay)) !important;
  color: var(--status-warn) !important;
  border-color: var(--status-warn-border);
}
.q-notification.bg-info {
  background-color: color-mix(in srgb, var(--status-info) 6%, var(--surface-overlay)) !important;
  color: var(--status-info) !important;
  border-color: var(--status-info-border);
}

/* 开关 / 复选框 / 单选：颜色经 --q-primary 重定向为强调色 */
.q-checkbox__inner,
.q-toggle__inner { color: var(--text-tertiary); }
.q-checkbox__label,
.q-toggle__label,
.q-radio__label { color: var(--text-primary); }

/* 深色下中性徽标对比度补足（语义色徽标不动：红/黄/绿仍是状态信号）。
   Quasar 的 .text-grey-7/8 落在 quasar_importants 层且带 !important
   （#757575/#424242 在深色内容面上仅 3.37:1 / 2.51:1，S2）；本表为非
   分层样式，必须同样 !important 才能赢过该层。 */
@media (prefers-color-scheme: dark) {
  .q-badge--outline.text-grey-7,
  .q-badge--outline.text-grey-8 {
    color: var(--text-secondary) !important;
    border-color: var(--hairline-strong);
  }
}

/* ==========================================================================
   8. 窄屏：只收紧间距令牌（不删减内容）
   ========================================================================== */
@media (max-width: 760px) {
  :root {
    --space-4: 12px;
    --space-5: 16px;
    --space-6: 24px;
    --toolbar-height: 44px;
  }
}
@media (max-width: 480px) {
  :root {
    --space-3: 8px;
    --space-4: 10px;
    --space-5: 12px;
    --space-6: 16px;
  }
  .app-toolbar { gap: var(--space-1); }
  .app-toolbar-spacer { min-width: 0; }
  .icon-btn { min-width: 30px; min-height: 30px; }
}

/* ==========================================================================
   9. 降级动效
   ========================================================================== */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    transition-duration: 0.001ms !important;
    animation-duration: 0.001ms !important;
    animation-iteration-count: 1 !important;
    scroll-behavior: auto !important;
  }
}

/* ==========================================================================
   10. Round 2 追加：列表行 / 徽标 / 面板头脚 / 详情分区 / 对话框骨架。
   本节只新增类；对既有钩子 .gate-error / .gate-warn 仅在这里追加覆盖规则
   （钩子类名不变），把“输入框外再套一个框”改为“输入控件本身染边”。
   ========================================================================== */
:root { --dialog-list-max: 16rem; }
@media (max-height: 620px) { :root { --dialog-list-max: 9rem; } }
@media (max-width: 480px) { :root { --dialog-list-max: 7rem; } }

/* --- 列表行（侧栏文件 / 条目列表共用；选中不依赖颜色：前置强调条+字重） --- */
.list-row {
  position: relative;
  display: flex;
  align-items: center;
  flex-wrap: wrap;          /* S12：窄面板下徽标换行，行内不横向溢出 */
  gap: var(--space-1);
  width: 100%;
  min-width: 0;
  padding: var(--space-1) var(--space-2);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background-color var(--motion-fast) var(--ease-standard);
}
.list-row:hover { background-color: var(--surface-tint); }
/* 可聚焦行（S8）：roving tabindex 由面板按选中项设置——Tab 只落一个
   站点、方向键仍是主路径；焦点环走令牌，不改变行内布局。 */
.focusable-row:focus-visible {
  outline: none;
  box-shadow: var(--ring-focus);
}
.list-row-active { background-color: var(--accent-soft); }
.list-row-active::before {
  content: "";
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 60%;
  min-height: 14px;
  border-radius: var(--radius-pill);
  background-color: var(--accent-text);   /* 软底上也 ≥3:1（S5） */
}
.list-row-name { color: var(--text-primary); min-width: 0; }
.list-row-active .list-row-name {
  color: var(--accent-text);              /* 软底上 4.85:1（S5） */
  font-weight: var(--fw-title);
}
/* 未加载 / 只读：单一弱化（名称转弱色），不再叠加多层 opacity */
.list-row-quiet .list-row-name { color: var(--text-tertiary); }

/* --- 徽标（文字即状态；颜色/虚线只是强化，两种配色下同令牌） --- */
.badge {
  /* inline-block（而非 inline-flex）：S12 收缩时 text-overflow 省略号
     生效（flex 容器不渲染省略号）；徽标现均为单一文字标签。 */
  display: inline-block;
  vertical-align: middle;
  flex: 0 1 auto;           /* S12：空间不足时可收缩（省略号截断） */
  min-width: 0;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  padding: 0 var(--space-1);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  background-color: var(--surface-sunken);
  color: var(--text-secondary);
  font-size: var(--fs-meta);
  line-height: 1.6;
  white-space: nowrap;
}
.badge-warn {
  border-color: var(--status-warn-border);
  background-color: var(--status-warn-bg);
  color: var(--status-warn);
}
.badge-error {
  border-color: var(--status-error-border);
  background-color: var(--status-error-bg);
  color: var(--status-error);
}
.badge-inferred {
  border-style: dashed;
  border-color: var(--status-warn-border);
  background-color: transparent;
  color: var(--status-warn);
}

/* --- 面板头 / 脚（标题与计数固定，列表自身滚动） --- */
.pane-head { flex: 0 0 auto; }
.pane-foot {
  flex: 0 0 auto;
  border-top: 1px solid var(--hairline);
  padding-top: var(--space-2);
}
.pane-title-row {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  min-width: 0;
}

/* --- 详情分区：主控制在前，次要以分隔线退后 --- */
.detail-section {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  min-width: 0;
}
.detail-section + .detail-section {
  border-top: 1px solid var(--hairline);
  padding-top: var(--space-3);
}

/* 只读值 / 依据原文展示盒（下沉底 + 令牌圆角内边距；长 token 任意断行） */
.code-box {
  border-radius: var(--radius-sm);
  background-color: var(--surface-sunken);
  padding: var(--space-2);
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

/* --- 对话框统一骨架（四框同宽同节奏；列表滚动上限走令牌） --- */
.dialog-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  width: min(34rem, calc(100vw - 2 * var(--space-4)));
}
.dialog-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
}
.dialog-body { overflow-wrap: break-word; min-width: 0; }
.dialog-list {
  max-height: var(--dialog-list-max);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  background-color: var(--surface-sunken);
  padding: var(--space-2);
}
.dialog-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-1);
}

/* --- 门控钩子覆盖：不画第二层框，改给内部控件染边（文字/图标在原因行） --- */
.gate-error, .gate-warn { border: none; background: transparent; padding: 0; }
.gate-error .q-field--outlined .q-field__control:before { border-color: var(--status-error); }
.gate-error .q-field--outlined.q-field--focused .q-field__control:after {
  border-color: var(--status-error);
}
.gate-error .q-toggle__inner { color: var(--status-error); }
.gate-warn .q-field--outlined .q-field__control:before { border-color: var(--status-warn); }
.gate-warn .q-field--outlined.q-field--focused .q-field__control:after {
  border-color: var(--status-warn);
}
.gate-warn .q-toggle__inner { color: var(--status-warn); }
"""


# ---------------------------------------------------------------------------
# 安装与令牌字符串助手
# ---------------------------------------------------------------------------


def install() -> None:
    """注入样式与 head 片段，并把明暗交给浏览器（build_gui 调用一次）。

    ``shared=True``：样式进首屏 HTML（避免 add_css 的运行时注入闪烁）。
    ``ui.dark_mode(None)``：Quasar 不钉死明暗，跟随 ``prefers-color-scheme``
    （与 :data:`CSS` 的深色分支同一媒体特性）；实际落地色全部由本表驱动。
    """
    ui.add_head_html(f"<style>{CSS}</style>", shared=True)
    ui.add_head_html(HEAD_HTML, shared=True)
    ui.dark_mode(None)


def space(n: int) -> str:
    """间距令牌引用，例 ``space(3)`` → ``var(--space-3)``。"""
    return f"var(--space-{n})"


def pad(n: int, x: int | None = None) -> str:
    """内边距声明：``pad(3)`` 或 ``pad(3, 4)``（纵向 / 横向）。"""
    if x is None:
        return f"padding: var(--space-{n})"
    return f"padding: var(--space-{n}) var(--space-{x})"


def gap(n: int) -> str:
    """flex 间距声明：``gap(2)`` → ``gap: var(--space-2)``。"""
    return f"gap: var(--space-{n})"