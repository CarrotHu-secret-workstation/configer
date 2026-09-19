# testdata/ 金样本说明

本目录两份文件是 configer 规范 §0.4 定义的**金样本**拷贝，作为回归测试的固定输入。

| 文件 | 来源绝对路径 |
|---|---|
| `param.py` | `/home/pi/Workspace/Prj/robocup_RLBrain/BehaviorTree_based_code/sim-3v3-stable-change6o3B/src/param.py` |
| `config.yaml` | `/home/pi/Workspace/Prj/robocup_physical_demo/K1_5v5_RLBrain_demo/K1_5v5_Demo_1.5/src/brain/config/config.yaml` |

- 拷贝日期：2026-09-08
- 对应规范版本：`docs/spec.md` v0.4
- sha256（拷贝时与源文件逐字节一致，已由 `tests/test_testdata.py` 校验）：
  - `param.py`：`fc409d2afeb472ce94012cb71c29d7e6fde4405e61f34aa24a785ed0fe0f11b8`
  - `config.yaml`：`73684a2707aafe669efacc7edcb48e7ce5cf5b44cb4c3a7f0b32d767b39b984d`

## 用途与约束

- 用途：M1/M2 验收测试（规范 §7.3 T1–T4、§11 A-1..A-16）的固定输入。
- **必须保持字节级不动**：不得编辑、格式化、换行符转换或重新保存这两份文件；
  任何改动都会破坏恒等往返测试（T1）与验收计数基准（§11）。
- 若源文件演进需要更新拷贝，必须同步修订规范 §11 的计数核对值并在本 README 更新
  拷贝日期与 sha256。
