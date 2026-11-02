import copy
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .model import InvalidModelOutput, ModelError
from .schemas import DEFAULT_MAX_ROUNDS, ValidationError, dumps, fingerprint, normalize, parse_time, validate_action
from .tools import result
from .diagnostics import public_value


def utcnow():
    return datetime.now(timezone.utc)


class Trace:
    def __init__(self, path, mode, model_id, run_id):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("x", encoding="utf-8")
        self.mode, self.model_id, self.run_id, self.seq = mode, model_id, run_id, 0

    def emit(self, kind, state, payload, iteration=0):
        self.seq += 1
        event_id = self.run_id + ":" + str(self.seq)
        event = dict(schema_version=1, event_id=event_id, seq=self.seq,
                     task_id=state["task"].get("task_id"), run_id=self.run_id,
                     timestamp=utcnow().isoformat(), mode=self.mode, model_id=self.model_id,
                     event_type=kind, iteration=iteration, plan_version=state.get("plan_version"),
                     task_status=state.get("status"), payload=payload)
        self.stream.write(dumps(event) + "\n")
        self.stream.flush()
        return event_id

    def close(self):
        self.stream.close()

    def model_request(self, body, state, iteration):
        directory = self.path.parent / "model-requests"
        directory.mkdir(exist_ok=True)
        path = directory / ("%02d.json" % iteration)
        with path.open("xb") as stream:
            stream.write(body)
        self.emit("model_request", state, {
            "request_body_ref": path.relative_to(self.path.parent).as_posix(),
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "observed_event_ids": [o["event_id"] for o in state["observations"]],
            "note": "实际交给 HTTP 的 JSON 正文，不含认证头；不是服务端接收日志"
        }, iteration)


def response(state, replayed=False, error=None, missing=None):
    plan = state.get("saved_plan")
    estimates = list(state.get("estimates", {}).values())
    quote = plan["estimate"] if plan else (estimates[-1] if estimates else {})
    return dict(task_id=state["task"].get("task_id"), status=state.get("status"), replayed=replayed,
                mode=state["mode"], missing_fields=missing or [], error=error,
                result=dict(plan=plan, steps=state.get("steps", []), tool_results=state.get("history", []),
                            estimated_total_cost=quote.get("estimated_total_cost"), duration_minutes=quote.get("duration_minutes"),
                            projected_finish_at=quote.get("projected_finish_at"), saved_plan_ref=state.get("saved_plan_ref"),
                            incomplete_reason=state.get("incomplete_reason"), gaps_by_option=state.get("gaps_by_option", []),
                            artifacts={k: state.get(k, {}) for k in ("materials", "scripts", "estimates")}))


class Agent:
    def __init__(self, store, tools, model_factory, mode, model_id, trace, max_rounds=DEFAULT_MAX_ROUNDS, clock=utcnow, fixed_clock=False):
        if type(max_rounds) is not int or not 1 <= max_rounds <= 100:
            raise ValidationError("max_rounds 必须在 1–100 之间")
        self.store, self.tools, self.model_factory = store, tools, model_factory
        self.mode, self.model_id, self.trace = mode, model_id, trace
        self.max_rounds, self.clock, self.fixed_clock = max_rounds, clock, fixed_clock

    def run(self, raw):
        state = {"task": raw if isinstance(raw, dict) else {}, "mode": self.mode, "status": None}
        try:
            task, missing = normalize(raw)
        except ValidationError as exc:
            self.trace.emit("input_checked", state, {"error": str(exc)})
            return response(state, error={"code": "invalid_input", "message": str(exc)})
        if missing:
            state["status"] = "needs_input"
            self.trace.emit("input_checked", state, {"missing_fields": missing})
            return response(state, missing=missing)
        state["task"] = task
        self.trace.emit("input_checked", state, {"normalized_input": task, "missing_fields": [],
                                                "fixed_clock": self.clock().isoformat() if self.fixed_clock else None})
        previous = self.store.get(task["task_id"])
        if previous:
            if previous["fingerprint"] != fingerprint(task):
                self.trace.emit("input_conflict", state, {"code": "task_id_conflict", "fields": [k for k in task if task[k] != previous["task"].get(k)]})
                return response(state, error={"code": "task_id_conflict", "message": "同一 task_id 的业务输入不同"})
            self.trace.emit("task_replayed", previous, {"original_run_id": previous["run_id"], "replayed": True,
                                                      "saved_plan_ref": previous.get("saved_plan_ref"), "resume_supported": False})
            return response(previous, replayed=True)
        state.update(fingerprint=fingerprint(task), run_id=self.trace.run_id, status="running", plan_version=0,
                     model_id=self.model_id, materials={}, scripts={}, estimates={}, steps=[], history=[], observations=[],
                     saved_plan=None, database_ref="sqlite:" + self.store.path.as_posix())
        self.store.create(state)
        if self.clock() > parse_time(task["deadline"]):
            return self.finish(state, "deadline_passed", "截止时间已过", 0)
        try:
            model = self.model_factory()
        except ModelError as exc:
            self.trace.emit("model_error", state, {"message": str(exc), "validation": "not_run"})
            return self.finish(state, "model_unavailable", str(exc), 0)
        for iteration in range(1, self.max_rounds + 1):
            if hasattr(model, "request_observer"):
                model.request_observer = lambda body: self.trace.model_request(body, state, iteration)
            context = {"task": task, "state": copy.deepcopy(state), "catalog": self.tools.catalog,
                       "remaining_rounds": self.max_rounds - iteration + 1, "iteration": iteration,
                       "max_rounds": self.max_rounds, "current_time": self.clock().isoformat()}
            raw_action = None
            try:
                raw_action = model.decide(context)
                self.trace.emit("model_output", state, getattr(model, "last_public_output", None) or {
                    "parsed_action": public_value(raw_action), "parse_ok": True, "source": "test_double"}, iteration)
                action = validate_action(raw_action)
            except (InvalidModelOutput, ValidationError) as exc:
                diagnostic = getattr(model, "last_public_output", None)
                if isinstance(exc, InvalidModelOutput):
                    self.trace.emit("model_output", state, diagnostic or {"parse_ok": False, "parse_issues": exc.issues}, iteration)
                rejected = public_value(raw_action, (getattr(model, "key", ""),)) if raw_action is not None else (diagnostic or {}).get("content")
                payload = {"code": "invalid_action", "stage": "parse" if isinstance(exc, InvalidModelOutput) else "schema_validation",
                           "message": str(exc), "issues": public_value(exc.issues, (getattr(model, "key", ""),)),
                           "rejected_action": rejected, "validation_ok": False, "tool_executed": False}
                event_id = self.trace.emit("action_rejected", state, payload, iteration)
                state["observations"].append({"event_id": event_id, **payload})
                self.store.persist(state)
                continue
            except ModelError as exc:
                self.trace.emit("model_error", state, {"message": str(exc)}, iteration)
                return self.finish(state, "model_error", str(exc), iteration)
            self.trace.emit("program_action" if getattr(model, 'action_source', 'model') == 'program' else "model_action", state, {**action, "observed_event_ids": [o["event_id"] for o in state["observations"]],
                                                  "validation_ok": True, "model_metadata": getattr(model, "last_metadata", {})}, iteration)
            if action["type"] == "finish":
                return self.finish(state, "model_stopped", action["decision_summary"], iteration,
                                   preserve_stop=getattr(model, "has_remaining_candidate", False))
            tool, args = action["tool"], action["arguments"]
            call_id = self.trace.run_id + "-call-" + str(iteration)
            before = copy.deepcopy(state)
            step = dict(step_id=call_id, call_id=call_id, tool=tool, arguments=args, status="running",
                        depends_on=self.dependencies(state, tool, args), plan_version=state["plan_version"],
                        result_ref=None, active_in_current_plan=True)
            state["steps"].append(step)
            self.store.persist(state)
            started = utcnow().isoformat()
            try:
                outcome = self.tools.execute(tool, args, state)
            except (OSError, ValueError, TypeError):
                state = before
                state["steps"].append(step)
                outcome = result("error", "tool_error", "工具执行异常，保留此前结果")
            outcome["meta"] = dict(call_id=call_id, task_id=task["task_id"], catalog_version=self.tools.catalog["catalog_version"],
                                   started_at=started, finished_at=utcnow().isoformat())
            self.record(state, step, tool, args, outcome)
            plan = None
            if tool == "save_plan" and outcome["status"] == "success":
                plan = state["saved_plan"]
                state["status"] = "completed"
                state["saved_plan_ref"] = outcome["data"]["saved_plan_ref"]
            try:
                self.store.persist(state, plan=plan)
            except sqlite3.Error:
                state = before
                step["status"] = "failed"
                state["steps"].append(step)
                outcome = result("error", "storage_error", "事务提交失败，方案未保存")
                outcome["meta"] = dict(call_id=call_id, task_id=task["task_id"], catalog_version=self.tools.catalog["catalog_version"],
                                       started_at=started, finished_at=utcnow().isoformat())
                self.record(state, step, tool, args, outcome)
                self.store.persist(state)
            event_id = self.trace.emit("tool_result", state, {"call_id": call_id, "step_id": step["step_id"], "tool": tool,
                                                            "arguments": args, "result": outcome, "summary": outcome["message"],
                                                            "step_status": step["status"]}, iteration)
            state["observations"].append({"event_id": event_id, "tool": tool, "result": outcome})
            self.store.persist(state)
            if state["status"] == "completed":
                self.trace.emit("task_finished", state, {"status": "completed", "saved_plan_ref": state["saved_plan_ref"],
                                                        "estimated_total_cost": outcome["data"]["estimated_total_cost"]}, iteration)
                return response(state)
        return self.finish(state, "step_limit", "达到最大执行轮数，未保存方案", self.max_rounds)

    @staticmethod
    def dependencies(state, tool, args):
        ids = []
        for h in state["history"]:
            data = h["result"].get("data") or {}
            used = ((tool == "generate_script" and h["tool"] == "search_materials" and any(m["material_id"] in args["material_ids"] for m in data.get("materials", []))) or
                    (tool in ("estimate_plan", "save_plan") and args.get("script_id") and data.get("script_id") == args["script_id"]) or
                    (tool == "save_plan" and data.get("estimate_id") == args["estimate_id"]))
            if used:
                ids.append(h["call_id"])
        return ids

    @staticmethod
    def record(state, step, tool, args, outcome):
        step["status"] = {"success": "succeeded", "partial": "partial", "error": "failed"}[outcome["status"]]
        step["result_ref"] = step["call_id"]
        step["plan_version"] = state["plan_version"]
        state["history"].append(dict(call_id=step["call_id"], tool=tool, arguments=args, result=outcome))
        for old in state["steps"]:
            if old["tool"] != "search_materials" and old["plan_version"] != state["plan_version"]:
                old["active_in_current_plan"] = False

    def finish(self, state, code, message, iteration, preserve_stop=False):
        # A model finish cannot authorize success. Only a persisted plan can.
        state["status"] = "incomplete"
        quotes = []
        for option_id in self.tools.catalog["options"]:
            try:
                q = self.tools.quote(state["task"], option_id)
                quotes.append({k: q[k] for k in ("option_id", "estimated_total_cost", "duration_minutes", "budget_gap", "time_gap_seconds", "missing", "feasible")})
            except (ValidationError, KeyError, TypeError):
                quotes.append({"option_id": option_id, "missing": ["invalid_catalog"], "feasible": False})
        # Preserve the primary reason for limits/errors; feasibility is separate diagnosis.
        diagnostic = None
        if any(q["missing"] for q in quotes):
            diagnostic = "insufficient_estimate_data"
        elif quotes and not any(q["feasible"] for q in quotes):
            diagnostic = "no_feasible_plan"
        if preserve_stop and code == "model_stopped":
            diagnostic = None
            quotes = []
        if code == "model_stopped" and diagnostic:
            code = diagnostic
        state["gaps_by_option"] = quotes
        state["incomplete_reason"] = {"code": code, "message": message, "feasibility_diagnosis": diagnostic,
                                      "overdue_seconds": max(0, (self.clock() - parse_time(state["task"]["deadline"])).total_seconds())}
        if code == "no_feasible_plan" and all(q["budget_gap"] > 0 for q in quotes):
            gap = min(q["budget_gap"] for q in quotes)
            state["incomplete_reason"].update(model_summary=message, minimum_budget_gap=gap,
                                              message=f"所有候选均超预算，最低预算缺口为 {gap} 个模拟单位。")
        self.store.persist(state)
        self.trace.emit("task_finished", state, {"status": state["status"], "reason": state["incomplete_reason"],
                                                "gaps_by_option": quotes, "retained_script_ids": list(state["scripts"])}, iteration)
        return response(state)
