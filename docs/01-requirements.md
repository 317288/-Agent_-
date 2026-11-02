# 需求与验收清单

用户是内容运营人员。目标是用已有素材生成活动计划、脚本及执行报价，在截止时间内保存满足预算和依赖约束的方案。交付形态是 Python CLI。项目规划预算为 180 分钟，收尾规划预算约 30–45 分钟；个人实际投入待本人填写。

| 题目要求 | 实现位置 | 验证用例 |
|---|---|---|
| 主题、预算、带时区截止时间、task_id；缺字段返回 needs_input | schemas.py、agent.py | 缺预算/截止时间，错误格式、过去和边界时刻 |
| 合成素材、费用和串行耗时；四种工具及 success/partial/error | data/、tools.py | 正常、部分素材/脚本、错误、费用非累计 |
| 模型选动作、观察后改选/确认/重试/停止 | staged.py、model.py、agent.py | 实际预算 12→8、临时失败后恢复 |
| 原动作、工具参数、返回摘要、来源、实际步骤状态 | agent.py、staged.py | 正常、缺信息、故障 Trace |
| 可行方案保存；不可行明确缺口；步数耗尽返回 incomplete | tools.py、agent.py | 正常、预算6、截止时间保存复核、step_limit |
| 步骤依赖、当前版本和脚本匹配，持久保存成功结果 | state.py、tools.py | 原失败未改写、SQLite事务失败、过期版本 |
| 相同输入重放，冲突拒绝，费用和产物不重复 | schemas.py、state.py、agent.py | 已完成任务重放/不同预算冲突 |
| 真实与离线入口、可复制运行方法及测试 | run.py、scripts/review.py、tests/、README | 真实 Ollama 批次与标准库 unittest |
| 同库代码、文档、实际证据及提交信息 | docs/、examples/review/ | 评估表、GitHub URL、最终 SHA |
| 失败续跑 | 加分项，本版未实现 | 不纳入必做验收 |

输入偏好服从预算、时间硬约束。成功仅指通过保存校验且数据库事务提交成功。真实模型的判断与确定性工程验证分别报告。[当前状态、证据及限制](03-evaluation.md)
