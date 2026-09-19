"""前端 GUI 层（规范 §10）——G1 骨架 + G2a 编辑语义 + G2b 对话框/快捷键 + G2c 接缝收口，全项目代码完工。

模块划分：``app``（run_gui/GuiOptions 接缝契约 + on_handoff_open 转交回调
注册器 + 退出拦截接线）、``state``（GuiState 状态容器 + 提交/冲突/关闭/退出
编排 + U-13 转交队列/U-10 失败重载）、``logic``（展示纯函数层，无 nicegui
依赖，无头可测）、``commit``（G2a：防抖状态机 DebounceManager + 提交/门控
结果→UI 动作映射 + U-6 控件选型纯函数，零 nicegui 依赖）、``dialogs``（G2b：
U-9 横幅状态机 / §7.6 冲突模态与强制覆盖二次警示文案 / U-12 关闭分流 /
§7.7 退出 flush 决策树 / U-11 快捷键注册表，纯函数零 nicegui 依赖）、
``modals``（G2b：数据驱动对话框宿主 ModalHost）、``layout``（U-1 IDE 布局/
刷新模型/事件消费/横幅条/错误条/快捷键派发/退出按钮/防抖 timer）、
``sidebar``（U-2 + U-12 关闭入口：悬停 ×/右键菜单）、``items``（U-3）、
``detail``（U-4/U-5/U-6 编辑控件/U-8/§7.6 gone 只读态）。G2c 已接：
CLI/session 接缝（启动批次失败文件诊断态、单实例转交经 handoff 队列进
GuiState、per-file 事件公开 API 正规化）。

技术栈决议（附录 B）：NiceGUI 双模式——开发/验收用浏览器模式（``ui.run()``，
默认仅绑 127.0.0.1 随机端口，远程走 SSH 端口转发，``--host``/``--port`` 显式
覆盖）；桌面形态用 native 模式（``ui.run(native=True)``，底座 pywebview），
两种模式前端代码完全相同。界面文案统一简体中文（v0.4 决议）。

需求清单 U-1..U-13（§10，M2 验收基准 §11）：IDE 布局与可收起侧栏、多文件
会话、条目列表（分组/子分组折叠 + 搜索）、详情面板、推测徽标（C-1/I-7）、
类型控件映射与门控反馈（红标拦截/黄标放行）、实时提交与撤销（无保存按钮）、
readonly 灰显 / dormant 黄标可编辑 / warning 黄标（U-8，v0.4）、外部修改
横幅与冲突模态、加载失败诊断态、键盘操作、关闭文件、单实例转交提示。

层间边界（§2 第 1 条）：前端只消费 §3 数据结构（ConfigDoc/ConfigItem/
Diagnostic，见 configer.model），**不得**出现「这是 YAML」之类格式分支；
展示规则由语义模型字段驱动（§3.5）。
"""
