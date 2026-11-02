# 最终验收结果

最终评审入口为 `python -X utf8 scripts/review.py --case normal`。运行前固定四例，分别使用新任务、独立运行目录和数据库，各运行一次。模型为本地 Qwen2.5 7B，原生 Schema，10 步。初次批次后仅修复无可行最终说明误用模型摘要的问题，随后复测该项一次。共有 **5 次真实任务运行**，其他三项没有重跑。[批次参数与源文件哈希](../examples/review/batch.json) · [定向复测版本](../examples/review/gap-fix-batch.json)

| 验收点 | 最终状态 | 证据 | 限制 |
|---|---|---|---|
| 正常确认与保存 | completed，图文8单位、60分钟。模型3次、工具6次；实际核对数据库、导出、费用和依赖 | [验证](../examples/review/normal/verification.json) | 单次运行，程序组织制作步骤 |
| 超预算后模型改选 | 通过。视频12超过10，实际改选图文8，模型确认后成功保存 | [Trace](../examples/review/budget/trace.jsonl) | 不据摘要猜测或替换参数 |
| 工具错误与恢复 | 正确处理失败，且最终恢复保存。仅首次脚本生成失败；原素材保留，失败步骤仍 failed | [验证](../examples/review/fault/verification.json) · [Trace](../examples/review/fault/trace.jsonl) | 模型再次确认同一真实报价，没有输出 retry 字面值 |
| 全部超预算 | 两次均取得视频12、图文8的实际报价后模型 stop；未保存，incomplete/no_feasible_plan。复测最终说明最低缺口2 | [原输出](../examples/review/infeasible-first/output.json) · [修正后](../examples/review/infeasible/output.json) | 两次模型摘要均误报6；保留在 model_summary，程序根据费用复核2 |
| 缺预算/截止时间 | needs_input，分别列出 budget、deadline。0模型/工具调用 | [旧版真实CLI前置验证](../examples/review/prior-checks/results.json) | 复用上一阶段记录，最终改动在前置校验之后 |
| 重放及冲突 | 同输入返回已有完成结果，成本仍12，保存数量1→1；不同预算明确拒绝，无新增模型/工具执行 | [重放及冲突核对](../examples/review/prior-checks/results.json) | 同上，幂等路径未改，单进程 |
| 预算/时间计算、保存依赖与执行上限 | 自动测试通过，含保存时钟变化、恰好可行、非法确认、旧版本估算、重复请求被拒绝且 step_limit、程序阶段不能绕过上限 | [59项最终回归](../examples/review/tests.txt) | 确定性测试与模型决策分开计数 |

**观察到的决策路径。** 预算任务第2步视频报价12，缺口2；第3步模型选择 evaluate_option(graduation_slides)，取得报价8。第4步模型用真实 estimate_id 确认，随后程序生成脚本、绑定估算，第6步保存。故障任务第4步 generate_script 返回 injected_failure。第5步模型再次选择 proceed_with_plan，程序据此发起独立的脚本调用并成功；第7步保存。两条路径均可从 Trace 中比较 model_choice、参数补全和 tool_result，而非从程序分支推断模型决策。

**原批次缺陷与修复。** 初次无可行输出中逐方案缺口为6、2，但最终说明直接照录模型误说的“至少缺6”。保留初次原始记录。修复只影响 no_feasible_plan 的展示原因，依据完整目录最低预算缺口生成 message 与 minimum_budget_gap，并保留原始模型摘要。新增针对该问题的测试，59项全部通过，再执行唯一一次定向复测。其余三个成功路径不经过这个结束分支。模型表述错误仍然存在。

**历史版本单列。** 提交前的冻结示例提示版本四例为1通过、3失败：首选可行保存成功，原预算改选、更名替代、无可行场景未达标。历史批次没有排除已经报价的候选，仍可能反复估算。这些成绩不混入最终批次。四例输入、真实公开动作及全部失败证据见 [历史批次](../examples/review/history-few-shot/)。更早开发试验保留在原工作区，提交包精选评审所需记录。评审副本仅将运行机器绝对项目根路径替换为 `<project>`，保留实际 ID、参数、业务返回与顺序，实际模型请求文件按原字节复制。
