import argparse
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from .agent import Agent, Trace, utcnow
from .model import LiveModel, MockModel
from .schemas import DEFAULT_MAX_ROUNDS, TOOL_SPECS, ValidationError, parse_time
from .state import Store
from .tools import BusinessTools


ROOT = Path(__file__).resolve().parents[2]


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="内容任务规划 Agent；Mock 仅验证流程")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--mode", choices=["mock", "live"], default="mock")
    parser.add_argument("--staged", action="store_true", help="阶段骨架：常规组织由程序负责，方案/异常选择由模型负责")
    parser.add_argument("--db", type=Path, default=Path("runtime/tasks.sqlite3"))
    parser.add_argument("--output-dir", type=Path, help="本次运行新目录，不覆盖已有证据")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    parser.add_argument("--now", help="仅 Mock 测试固定时钟，必须带时区")
    parser.add_argument("--fail-once", choices=list(TOOL_SPECS))
    parser.add_argument("--partial-once", choices=["search_materials", "generate_script"])
    args = parser.parse_args(argv)
    if args.mode == "live" and args.now:
        parser.error("live 模式禁止固定时钟，使用实际当前时间")
    if not 1 <= args.max_rounds <= 100:
        parser.error("max-rounds 必须在 1–100 之间")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    run_id = uuid4().hex
    out = args.output_dir or Path("runtime/runs") / run_id
    trace, store = None, None
    try:
        fixed = parse_time(args.now) if args.now else None
        clock = (lambda: fixed) if fixed else utcnow
        raw = json.loads(args.input.read_text(encoding="utf-8-sig"))
        out.mkdir(parents=True, exist_ok=False)
        write_json(out / "input.json", raw)
        write_json(out / "run-config.json", {"mode": args.mode, "max_rounds": args.max_rounds, "fixed_now": args.now,
                                            "fail_once": args.fail_once, "partial_once": args.partial_once,
                                            "command": ["python", "run.py"] + (argv if argv is not None else sys.argv[1:]),
                                            "run_id": run_id})
        model_id = "offline-rule-mock-v1" if args.mode == "mock" else os.environ.get("AGENT_MODEL", "unconfigured")
        trace = Trace(out / "trace.jsonl", args.mode, model_id, run_id)
        store = Store(args.db)
        tools = BusinessTools(args.data_dir, clock, args.fail_once, args.partial_once,
                              fault_task_id=raw.get("task_id") if isinstance(raw, dict) else None)
        factory = MockModel if args.mode == "mock" else LiveModel
        if args.staged:
            if args.mode != 'live':
                raise ValueError('--staged requires explicit live mode')
            from .staged import StagedModel
            factory = lambda: StagedModel(LiveModel(), trace)
        agent = Agent(store, tools, factory,
                      args.mode, model_id, trace, args.max_rounds, clock, fixed_clock=bool(fixed))
        output = agent.run(raw)
        output["run_id"] = run_id
        output["trace_path"] = str((out / "trace.jsonl").resolve())
        if output["status"] == "completed":
            plan = store.get_plan(output["task_id"])
            if plan is None:
                raise RuntimeError("完成状态缺少已保存方案")
            write_json(out / "plan.json", plan)
            output["exported_plan_path"] = str((out / "plan.json").resolve())
        write_json(out / "output.json", output)
        code = 5 if (output.get("error") or {}).get("code") == "task_id_conflict" else {"completed": 0, "needs_input": 2, "incomplete": 3, "running": 3}.get(output["status"], 4)
        write_json(out / "exit.json", {"exit_code": code})
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return code
    except (OSError, ValueError, ValidationError) as exc:
        print(json.dumps({"error": {"code": "cli_error", "message": str(exc)}, "note": "检查数据库中的权威状态；证据/导出可能未完整写入"}, ensure_ascii=False))
        return 4
    finally:
        if trace:
            trace.close()
        if store:
            store.close()
