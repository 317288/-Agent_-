# 设计与接口

CLI 输入 task_id、theme、非负整数 budget、带时区 deadline 及可选 instructions。规范化输入生成指纹。相同任务相同输入返回持久结果，不重复执行；不同输入返回 task_id_conflict。任务状态为 running、needs_input、completed、incomplete，输入错误不创建任务。生成计划中的活动步骤仍为 planned；Agent 工具尝试分别记录 running、succeeded、partial、failed。

| 工具 | 输入 | 返回与依赖 |
|---|---|---|
| search_materials | query、required_kinds | 合成素材 ID、种类及缺失项 |
| generate_script | option_id、material_ids | 脚本 ID、版本、章节与素材引用；需已检索的真实素材 |
| estimate_plan | option_id，可选 script_id | estimate_id、各项费用、串行耗时、缺口、feasible、save_ready；候选报价不等于可保存估算 |
| save_plan | plan_version、script_id、estimate_id | 重新校验时间、预算、脚本、目录及步骤依赖，事务提交后返回保存引用 |

每次工具调用都产生 success、partial 或 error 的真实结果。候选估算执行成功也可能超预算。活动费用来自单个选项费用表，重复查询不产生支出。

## 最终阶段入口

初始素材检索由程序安排。模型看到真实返回及可用动作，选择 evaluate_option、proceed_with_plan、retry 或 stop。每次模型选择与后续工具执行均由同一个最多10步的循环控制。没有内部重试循环。

候选报价复用范围是当前持久任务（预算/截止时间已由指纹固定）、方案与费用表版本。只接受该任务的成功候选记录，并核对持久产物。已报价方案不再进入评估枚举。每个当前可行报价生成一个与真实 option_id、estimate_id 精确配对的确认分支，时间按当前时刻加串行耗时复查。stop 始终可选，实际可重试错误/部分成功增加 retry。发给 Ollama 的 Schema 和本地校验来自同一组选项。模型选择失效参数会被拒绝，消耗轮次，不触发工具。候选未评估完时提前停止保留 model_stopped。

模型合法确认后，程序组织生成脚本、绑定脚本重新估算、校验并保存。绑定估算与候选报价用途不同。任一工具失败返回模型决策点。每步至多一次模型调用和一次工具执行。模型原文和结构化动作、程序安排、ID 补全、工具实际结果、阶段转换和最终状态分别存入 Trace；不采集内部思维或认证头。输出包含计划、工具结果、实际状态、费用和保存引用或未完成原因。

原生 Python 与 SQLite 足以复核依赖和持久化状态，无需额外框架。Ollama 不可用时 live 返回未完成，离线 Mock 需显式选择。
