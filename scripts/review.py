"""Generate a fresh task and verify the public CLI, stored plan and model choices."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from uuid import uuid4

from verify_stage4 import ROOT, INSTRUCTIONS, evaluate, write


CASES = {
    "normal": (20, None),
    "budget": (10, INSTRUCTIONS["A"]),
    "fault": (20, INSTRUCTIONS["B"]),
    "infeasible": (6, INSTRUCTIONS["C"]),
}


def run_case(case, directory):
    directory.mkdir(parents=True, exist_ok=False)
    task_id = f"review-{case}-{uuid4().hex[:12]}"
    budget, instructions = CASES[case]
    task = {"task_id": task_id, "theme": "校园毕业季", "budget": budget,
            "deadline": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}
    if instructions:
        task["instructions"] = instructions
    write(directory / "input.json", task)
    database = ROOT / "runtime" / (task_id + ".sqlite3")
    run_dir = directory / "run"
    command = [sys.executable, "-X", "utf8", "run.py", "--mode", "live", "--staged",
               "--max-rounds", "10", "--input", str(directory / "input.json"),
               "--db", str(database), "--output-dir", str(run_dir)]
    if case == "fault":
        command.extend(["--fail-once", "generate_script"])
    with (directory / "stdout.json").open("wb") as stdout:
        result = subprocess.run(command, cwd=ROOT, stdout=stdout)
    output = json.loads((run_dir / "output.json").read_bytes())
    events = [json.loads(line) for line in (run_dir / "trace.jsonl").read_bytes().splitlines()]
    with sqlite3.connect(str(database)) as db:
        row = db.execute("SELECT document FROM plans WHERE task_id=?", (task_id,)).fetchone()
        plan = json.loads(row[0]) if row else None
        snapshot = json.loads(db.execute("SELECT snapshot FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0])
    common = evaluate({"budget":"A", "infeasible":"C"}.get(case, "B"), output, events, plan, snapshot)
    checks = common["checks"]
    exported = run_dir / "plan.json"
    checks["export_matches"] = json.loads(exported.read_bytes()) == plan if plan else not exported.exists()
    choices = [(e["iteration"], e["payload"]["choice"]) for e in events if e["event_type"] == "model_choice"]
    tools = [e for e in events if e["event_type"] == "tool_result"]
    estimates = {e["payload"]["result"]["data"]["estimate_id"]: e for e in tools
                 if e["payload"]["tool"] == "estimate_plan" and e["payload"]["result"]["status"] == "success"}
    confirmed = [(i, a) for i, a in choices if a["action"] == "proceed_with_plan"
                 and a["estimate_id"] in estimates and estimates[a["estimate_id"]]["iteration"] < i
                 and estimates[a["estimate_id"]]["payload"]["result"]["data"]["feasible"]
                 and plan and plan["option_id"] == a["option_id"]]
    criteria = {} if case == "normal" else common["criteria"]
    if case == "infeasible":
        observed = {e["payload"]["arguments"]["option_id"] for e in estimates.values()
                    if e["payload"]["result"]["data"]["budget_gap"] > 0}
        criteria["all_candidates_observed"] = observed == {"graduation_video","graduation_slides"}
        passed = common["scenario_passed"] and criteria["all_candidates_observed"]
    else:
        criteria["explicitly_adopted_saved_option"] = bool(confirmed)
        passed = output["status"] == "completed" and bool(confirmed)
        if case != "normal":
            passed = passed and common["scenario_passed"]
    passed = passed and all(checks.values())
    report = {"case": case, "passed": passed, "task_id":task_id, "status":output["status"],
              "reason":output["result"]["incomplete_reason"], "cost":output["result"]["estimated_total_cost"],
              "cli_exit":result.returncode, "model_calls":common["model_calls"], "tool_calls":len(tools),
              "choices":choices, "checks":checks, "criteria":criteria}
    write(directory / "verification.json", report)
    print(f"{case}: {output['status']}, passed={passed}, evidence={directory}", flush=True)
    return passed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=[*CASES, "all"], default="normal")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    destination = (args.output or ROOT / "runtime" / ("review-" + uuid4().hex[:12])).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    for key, value in {"AGENT_MODEL":"qwen2.5:7b-instruct-q4_K_M", "AGENT_BASE_URL":"http://localhost:11434/v1",
                       "AGENT_NUM_CTX":"8192", "AGENT_PROTOCOL":"ollama", "AGENT_RESPONSE_FORMAT":"json_schema",
                       "AGENT_TIMEOUT_SECONDS":"180", "AGENT_API_KEY":""}.items():
        os.environ[key] = value
    cases = list(CASES) if args.case == "all" else [args.case]
    write(destination / "batch.json", {"cases":cases, "attempts_per_case":1, "max_steps":10,
          "expected":{"normal":"explicit adoption and saved plan", "budget":"12 over budget then model selects 8, confirms and saves",
                      "fault":"one injected script failure retained, independent model recovery and save",
                      "infeasible":"model stop after actual quotes, minimum gap 2, no save"},
          "source_sha256":{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in (ROOT / "src").rglob("*.py")}})
    results = [run_case(case, destination / case) for case in cases]
    sys.exit(0 if all(results) else 3)
