"""Run one real scenario and evaluate observations/actions, not just final status."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from content_agent.schemas import parse_time

MAX_ROUNDS = 10  # Fixed before stage-4 live testing, never increased on failure.
INSTRUCTIONS = {
    "A": "优先评估视频方案 graduation_video；预算不允许时接受图文方案 graduation_slides。请根据实际工具估算作决定，完成并保存可行的活动方案。",
    "B": "完成并保存一份校园毕业季活动方案；复用已经成功取得的素材和产物。",
    "C": "严格遵守预算；如没有可行方案，说明预算缺口并停止，不得保存超预算方案。",
}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def artifacts_retained(output):
    result = output["result"]
    artifacts = result["artifacts"]
    for h in result["tool_results"]:
        r, tool = h["result"], h["tool"]
        if r["status"] not in ("success", "partial") or not r["data"]:
            continue
        data = r["data"]
        if tool == "search_materials":
            if any(artifacts["materials"].get(m["material_id"]) != m for m in data["materials"]):
                return False
        if tool == "generate_script" and artifacts["scripts"].get(data["script_id"]) != data:
            return False
        if tool == "estimate_plan" and artifacts["estimates"].get(data["estimate_id"]) != data:
            return False
    return True


def evaluate(scenario, output, events, plan_in_db, snapshot):
    actions = [e for e in events if e["event_type"] == "model_action"]
    tools = [e for e in events if e["event_type"] == "tool_result"]
    requests = [e for e in events if e["event_type"] == "model_request"]
    checks = {"live_only": output["mode"] == "live" and all(e["mode"] == "live" for e in events),
              "bounded": len(requests) <= MAX_ROUNDS,
              "successful_artifacts_retained": artifacts_retained(output),
              "persisted_status_matches": snapshot["status"] == output["status"]}
    success = output["status"] == "completed"
    if success:
        p = plan_in_db
        checks["persisted_plan_matches_output"] = p == output["result"]["plan"] and p is not None
        if p:
            q, task = p["estimate"], p["task"]
            e = output["result"]["artifacts"]["estimates"][q["source_estimate_id"]]
            checks.update(cost_correct=sum(i["quantity"] * i["unit_cost"] for i in q["line_items"]) == q["estimated_total_cost"],
                          duration_correct=sum(i["quantity"] * i["duration_minutes"] for i in q["line_items"]) == q["duration_minutes"],
                          budget_ok=q["estimated_total_cost"] <= task["budget"], deadline_ok=parse_time(q["projected_finish_at"]) <= parse_time(task["deadline"]),
                          save_ready=q["save_ready"] and all(q["checks"].values()),
                          script_estimate_match=p["script"]["script_id"] == e["script_id"] and p["plan_version"] == e["plan_version"] and e["save_ready"])
    links = []
    observations = []
    for event in tools:
        r = event["payload"]["result"]
        d = r.get("data") or {}
        relevant = ((scenario == "A" and event["payload"]["tool"] == "estimate_plan" and d.get("option_id") == "graduation_video" and d.get("budget_gap", 0) > 0) or
                    (scenario == "B" and r["code"] == "injected_failure") or
                    (scenario == "C" and event["payload"]["tool"] == "estimate_plan" and d.get("budget_gap", 0) > 0))
        if not relevant:
            continue
        observations.append(event)
        later = [a for a in actions if a["iteration"] > event["iteration"] and event["event_id"] in a["payload"]["observed_event_ids"]]
        links.append({"observation_event_id": event["event_id"], "iteration": event["iteration"], "tool": event["payload"]["tool"],
                      "result_code": r["code"], "budget_gap": d.get("budget_gap"),
                      "subsequent_actions": [{"iteration": a["iteration"], "action": a["payload"]["type"], "tool": a["payload"].get("tool"),
                                              "arguments": a["payload"].get("arguments"), "decision_summary": a["payload"]["decision_summary"]} for a in later]})
    extra = {}
    if scenario == "A":
        replanned = any(a.get("arguments", {}).get("option_id") == "graduation_slides" for link in links for a in link["subsequent_actions"] if isinstance(a.get("arguments"), dict))
        passed = success and bool(observations) and replanned and plan_in_db["option_id"] == "graduation_slides"
        extra = {"feasible_plan_completed": success, "observed_video_over_budget": bool(observations), "model_chose_alternative_after_observation": replanned,
                 "direct_slides_without_over_budget_is_not_replanning": success and not observations}
    elif scenario == "B":
        failed_ids = [e["payload"]["call_id"] for e in observations]
        failure_steps = [s for s in output["result"]["steps"] if s["call_id"] in failed_ids]
        recovered_calls = [e for e in tools if observations and e["iteration"] > observations[0]["iteration"] and e["payload"]["tool"] == "generate_script" and e["payload"]["result"]["status"] == "success"]
        observed_recovery = any(a["tool"] == "generate_script" and a["iteration"] in [e["iteration"] for e in recovered_calls] for link in links for a in link["subsequent_actions"])
        handled = len(observations) == 1 and bool(failure_steps) and all(s["status"] == "failed" for s in failure_steps) and checks["successful_artifacts_retained"]
        extra = {"program_handled_failure_correctly": handled, "model_recovered_task": success and observed_recovery, "injected_failure_count": len(observations)}
        passed = handled and success and observed_recovery
    else:
        reason = output["result"]["incomplete_reason"] or {}
        quotes = output["result"]["gaps_by_option"]
        gaps = [q["budget_gap"] for q in quotes if q.get("budget_gap") is not None]
        program_correct = output["status"] == "incomplete" and plan_in_db is None and output["result"]["saved_plan_ref"] is None and reason.get("code") == "no_feasible_plan" and bool(gaps) and min(gaps) == 2
        observed_stop = any(a["action"] == "finish" for link in links for a in link["subsequent_actions"])
        extra = {"program_returned_verified_no_feasible_plan": program_correct, "minimum_budget_gap": min(gaps) if gaps else None,
                 "model_stopped_after_infeasible_tool_observation": observed_stop, "not_step_limit": reason.get("code") != "step_limit"}
        passed = program_correct and observed_stop
    return {"scenario": scenario, "checks": checks, "criteria": extra, "observation_action_links": links,
            "scenario_passed": bool(passed and all(checks.values())), "status": output["status"], "incomplete_reason": output["result"]["incomplete_reason"],
            "model_calls": len(requests), "actions": [{"iteration": e["iteration"], **e["payload"]} for e in actions]}


def run(scenario, directory):
    directory.mkdir(parents=True, exist_ok=False)
    identity = "stage4-" + scenario.lower() + "-" + uuid4().hex[:12]
    now = datetime.now(timezone(timedelta(hours=8)))
    task = {"task_id": identity, "theme": "校园毕业季", "budget": {"A": 10, "B": 20, "C": 6}[scenario],
            "deadline": (now + timedelta(days=1)).replace(microsecond=0).isoformat(), "instructions": INSTRUCTIONS[scenario]}
    write(directory / "task.json", task)
    db, evidence = ROOT / "runtime" / (identity + ".sqlite3"), directory / "run"
    command = [sys.executable, "-X", "utf8", str(ROOT / "run.py"), "--input", str(directory / "task.json"), "--mode", "live",
               "--db", str(db), "--output-dir", str(evidence), "--max-rounds", str(MAX_ROUNDS)]
    if scenario == "B":
        command += ["--fail-once", "generate_script"]
    config = {k: os.environ.get(k) for k in ("AGENT_BASE_URL", "AGENT_MODEL", "AGENT_PROTOCOL", "AGENT_RESPONSE_FORMAT", "AGENT_NUM_CTX", "AGENT_TIMEOUT_SECONDS")}
    write(directory / "invocation.json", {"command": command, "config": config, "max_rounds": MAX_ROUNDS, "scenario": scenario, "task_id": identity, "started_at": now.isoformat(),
                                          "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT / "src").rglob('*.py')}})
    print("Starting " + identity, flush=True)
    with (directory / "stdout.json").open("w", encoding="utf-8") as out, (directory / "stderr.txt").open("w", encoding="utf-8") as err:
        proc = subprocess.run(command, cwd=ROOT, stdout=out, stderr=err)
    output = json.loads((evidence / "output.json").read_text(encoding="utf-8"))
    events = [json.loads(line) for line in (evidence / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    with sqlite3.connect(str(db)) as conn:
        row = conn.execute("SELECT document FROM plans WHERE task_id=?", (identity,)).fetchone()
        plan = json.loads(row[0]) if row else None
        snapshot = json.loads(conn.execute("SELECT snapshot FROM tasks WHERE task_id=?", (identity,)).fetchone()[0])
    report = evaluate(scenario, output, events, plan, snapshot)
    request_checks = []
    for link in report["observation_action_links"]:
        for action in link["subsequent_actions"]:
            req = next(e for e in events if e["event_type"] == "model_request" and e["iteration"] == action["iteration"])
            body = (evidence / req["payload"]["request_body_ref"]).read_bytes()
            context = json.loads(json.loads(body)["messages"][1]["content"])
            observation = next(e for e in events if e["event_id"] == link["observation_event_id"])
            matching = [h for h in context["tool_history"] if h["call_id"] == observation["payload"]["call_id"]]
            request_checks.append(bool(matching) and matching[0]["result"] == observation["payload"]["result"] and hashlib.sha256(body).hexdigest() == req["payload"]["body_sha256"])
    report["checks"]["linked_observations_in_actual_requests"] = bool(request_checks) and all(request_checks)
    report["checks"]["export_consistent"] = json.loads((evidence / "plan.json").read_text(encoding="utf-8")) == plan if plan else not (evidence / "plan.json").exists()
    report["scenario_passed"] = report["scenario_passed"] and all(report["checks"].values())
    report.update(task_id=identity, exit_code=proc.returncode, finished_at=datetime.now(timezone.utc).isoformat())
    write(directory / "verification.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["scenario_passed"] else 3


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=list(INSTRUCTIONS))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    raise SystemExit(run(args.scenario, args.output.resolve()))
