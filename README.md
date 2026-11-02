# 内容任务规划 Agent · 校园毕业季

Python CLI 为运营人员在预算和截止时间内制定活动方案。模型选择候选、确认报价、处理错误或停止。程序执行工具并校验预算、时间、脚本和保存依赖。所有素材均为合成资料。

## 从干净仓库运行

在项目根目录使用 Python 3.9+（实测 3.9.15）。仅使用标准库，无需安装第三方包。

```powershell
python -X utf8 run.py --mode mock --input examples/inputs/normal.json --now 2026-11-01T12:00:00+08:00 --db runtime/review-mock.sqlite3
python -X utf8 scripts/review.py --case normal
python -X utf8 -m unittest discover -s tests -v
```

第一条是离线规则 Mock，只验证流程。第二条是真实评审入口，需要本机 Ollama 在 `localhost:11434` 已有 `qwen2.5:7b-instruct-q4_K_M`。脚本生成新 task_id、24 小时后的带时区截止时间及独立数据库，实际调用 `run.py --mode live --staged --max-rounds 10`。脚本内设置原生 Ollama 协议、JSON Schema、temperature=0、8192 上下文及 180 秒超时，无需密钥。新运行存放于 runtime/，终端打印证据目录。用 `--case budget`、`fault`、`infeasible` 或 `all` 验证其他场景。验证脚本的退出码表示验收是否通过，任务 CLI 的退出码记录在 verification.json。

直接接入 CLI 时通过环境变量配置，详见 [.env.example](.env.example)。程序不自动读取 .env。自定义输入包括 task_id、theme、budget、ISO 8601 带时区 deadline，可选 instructions 表达软偏好。预算只计算方案实际预计执行费用，估算不扣费。耗时按串行计算。

## 评审证据

| 场景 | 输入、输出、实际 Trace |
|---|---|
| 正常保存 | [输入](examples/review/normal/input.json) · [输出](examples/review/normal/output.json) · [Trace](examples/review/normal/trace.jsonl) · [方案](examples/review/normal/plan.json) |
| 缺预算 | [输入](examples/review/prior-checks/missing_budget/input.json) · [输出](examples/review/prior-checks/missing_budget/output.json) · [Trace](examples/review/prior-checks/missing_budget/trace.jsonl) |
| 脚本临时失败后恢复 | [输入](examples/review/fault/input.json) · [输出](examples/review/fault/output.json) · [Trace](examples/review/fault/trace.jsonl) |
| 超预算改选、主动停止 | [12→8 改选记录](examples/review/budget/verification.json) · [无可行方案](examples/review/infeasible/output.json) |

[验收表及历史版本说明](docs/03-evaluation.md) · [需求](docs/01-requirements.md) · [设计](docs/02-solution.md) · [AI 使用](docs/04-ai-usage.md) · [复盘](docs/05-retrospective.md)

**已知限制**：最终批次每个场景仅一次。模型在无可行场景中主动停止，但公开摘要把最低缺口 2 误说成 6。程序以工具费用表复核最终数值，并保留原文。脚本是模板，素材与费用均为合成数据，不执行真实发布。单进程 SQLite 支持幂等重放，不实现失败续跑。
