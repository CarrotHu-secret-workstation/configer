"""适配器层（规范 §4、§5）。

职责边界（§2 第 3 条）：格式嗅探、解析为 ConfigDoc、按 EditOp 做 token 级
替换并序列化为字节。适配器**不得**做 I/O 落盘（写盘由核心统一原子写，§7.4），
**不得**修改与其无关的语义。

- base：§4.1 Adapter Protocol（稳定接口）；
- python_adapter：§4.3（libcst），第二阶段实现；
- yaml_adapter：§4.4（ruamel.yaml round-trip），第二阶段实现。
"""
