"""Real Ollama stage-3 verification. Each invocation retains a fresh task and all evidence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from content_agent.schemas import parse_time


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get(url):
    with urllib.request.urlopen(url, timeout=15) as reply:
        return json.load(reply)


def probe(directory):
    directory.mkdir(parents=True, exist_ok=False)
    base = os.environ["AGENT_BASE_URL"].rstrip("/")
    server = base.removesuffix("/v1")
    info = {"checked_at": datetime.now(timezone.utc).isoformat(), "version": get(server + "/api/version"),
            "models": get(server + "/api/tags"), "before_ps": get(server + "/api/ps")}
    write(directory / "server.json", info)
    schema = {"type": "object", "properties": {"probe": {"type": "string", "enum": ["schema_ok"]}}, "required": ["probe"], "additionalProperties": False}
    body = {"model": os.environ["AGENT_MODEL"], "messages": [{"role": "user", "content": 'Return JSON {"probe":"wrong_value","extra":true}. Do not explain.'}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "capability_probe", "strict": True, "schema": schema}}, "temperature": 0}
    write(directory / "request.json", body)
    req = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            envelope = json.load(r)
        choice = envelope["choices"][0]
        content = choice["message"]["content"]
        output = {"public_content": content, "finish_reason": choice.get("finish_reason"), "served_model": envelope.get("model"), "usage": envelope.get("usage"),
                  "schema_probe_passed": json.loads(content) == {"probe": "schema_ok"}}
    except (urllib.error.URLError, ValueError, KeyError) as exc:
        output = {"schema_probe_passed": False, "error_type": type(exc).__name__}
    write(directory / "result.json", output)
    print(json.dumps(output, ensure_ascii=False), flush=True)
    return 0 if output["schema_probe_passed"] else 3


def run(directory):
    directory.mkdir(parents=True, exist_ok=False)
    identity = "stage3-" + uuid4().hex[:16]
    now = datetime.now(timezone(timedelta(hours=8)))
    task = {"task_id": identity, "theme": "校园毕业季", "budget": 20, "deadline": (now + timedelta(days=1)).replace(microsecond=0).isoformat()}
    write(directory / "task.json", task)
    db = ROOT / "runtime" / (identity + ".sqlite3")
    evidence = directory / "run"
    cmd = [sys.executable, "-X", "utf8", str(ROOT / "run.py"), "--input", str(directory / "task.json"),
           "--mode", "live", "--db", str(db), "--output-dir", str(evidence), "--max-rounds", "8"]
    write(directory / "invocation.json", {"command": cmd, "model": os.environ.get("AGENT_MODEL"), "base_url": os.environ.get("AGENT_BASE_URL"),
                                          "response_format": os.environ.get("AGENT_RESPONSE_FORMAT", "json_schema"), "timeout_seconds": os.environ.get("AGENT_TIMEOUT_SECONDS", "180"),
                                          "protocol": os.environ.get("AGENT_PROTOCOL", "chat_completions"), "num_ctx": os.environ.get("AGENT_NUM_CTX"),
                                          "temperature": 0, "started_at": now.isoformat(), "fault_injection": False})
    print("Starting " + identity, flush=True)
    with (directory / "stdout.json").open("w", encoding="utf-8") as stdout, (directory / "stderr.txt").open("w", encoding="utf-8") as stderr:
        process = subprocess.run(cmd, cwd=ROOT, stdout=stdout, stderr=stderr)
    output = json.loads((evidence / "output.json").read_text(encoding="utf-8"))
    try:
        server = os.environ["AGENT_BASE_URL"].rstrip("/").removesuffix("/v1")
        write(directory / "after-ps.json", get(server + "/api/ps"))
    except (urllib.error.URLError, ValueError):
        write(directory / "after-ps.json", {"unavailable": True})
    events = [json.loads(line) for line in (evidence / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    actions = [{"iteration": e["iteration"], "tool": e["payload"].get("tool", "finish"), "arguments": e["payload"].get("arguments"), "summary": e["payload"]["decision_summary"]} for e in events if e["event_type"] == "model_action"]
    checks = {}
    if output["status"] == "completed":
        with sqlite3.connect(str(db)) as conn:
            row = conn.execute("SELECT document FROM plans WHERE task_id=?", (identity,)).fetchone()
            snapshot = json.loads(conn.execute("SELECT snapshot FROM tasks WHERE task_id=?", (identity,)).fetchone()[0])
        plan = json.loads(row[0]) if row else None
        exported = json.loads((evidence / "plan.json").read_text(encoding="utf-8"))
        estimate = plan["estimate"]
        refs = {h["result"]["data"].get("estimate_id"): h["result"]["data"] for h in output["result"]["tool_results"] if h["tool"] == "estimate_plan" and h["result"]["data"]}
        source = refs[estimate["source_estimate_id"]]
        checks = {"database_plan_readable": bool(plan), "export_matches_database": exported == plan == output["result"]["plan"],
                  "completed_persisted": snapshot["status"] == "completed", "cost_sum_matches": sum(i["subtotal"] for i in estimate["line_items"]) == estimate["estimated_total_cost"],
                  "budget_ok": estimate["estimated_total_cost"] <= task["budget"],
                  "duration_sum_matches": sum(i["duration_minutes"] * i["quantity"] for i in estimate["line_items"]) == estimate["duration_minutes"],
                  "deadline_ok": parse_time(estimate["projected_finish_at"]) <= parse_time(task["deadline"]),
                  "checks_true": all(estimate["checks"].values()) and estimate["save_ready"],
                  "script_and_estimate_match": plan["script"]["script_id"] == source["script_id"] and source["plan_version"] == plan["plan_version"] and source["save_ready"],
                  "script_complete": not plan["script"]["missing_sections"],
                  "activity_steps_planned": all(s["status"] == "planned" for s in plan["activity_steps"])}
    requests = [e for e in events if e["event_type"] == "model_request"]
    checks["request_hashes_match"] = all(hashlib.sha256((evidence / e["payload"]["request_body_ref"]).read_bytes()).hexdigest() == e["payload"]["body_sha256"] for e in requests)
    result = {"task_id": identity, "status": output["status"], "exit_code": process.returncode, "actions": actions,
              "format_rejections": [e["payload"] for e in events if e["event_type"] == "action_rejected"],
              "tool_failures": [e["payload"] for e in events if e["event_type"] == "tool_result" and e["payload"]["result"]["status"] != "success"],
              "model_calls": len(requests), "verification": checks, "incomplete_reason": output["result"]["incomplete_reason"],
              "verified_success": output["status"] == "completed" and all(checks.values()), "finished_at": datetime.now(timezone.utc).isoformat()}
    write(directory / "verification.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result["verified_success"] else 3


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["probe", "run"])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit((probe if args.phase == "probe" else run)(args.output.resolve()))
