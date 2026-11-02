"""Bounded factual model view; full artifacts and history remain in storage."""
import copy
import json

HISTORY_GROUPS = 6
MAX_VIEW_BYTES = 16000  # UTF-8 bytes, explicitly NOT a token count.
ESTIMATE_FIELDS = ("estimate_id", "option_id", "script_id", "plan_version", "feasible", "save_ready",
                   "checks", "missing", "estimated_total_cost", "duration_minutes", "budget_gap", "time_gap_seconds", "evaluated_at")


def pick(value, keys):
    return {k: value[k] for k in keys if k in value}


def signature(tool, arguments):
    return json.dumps([tool, arguments], sort_keys=True, ensure_ascii=False)


def result_view(h, event_ids):
    """Project real fields only; never synthesize a successful result or quote."""
    r, tool = h["result"], h["tool"]
    data = r.get("data") or {}
    if tool == "search_materials":
        d = {"materials": [pick(m, ("material_id", "kind", "title")) for m in data.get("materials", [])]}
    elif tool == "generate_script":
        d = pick(data, ("script_id", "option_id", "plan_version", "material_ids", "missing_sections"))
    elif tool == "estimate_plan":
        d = pick(data, ESTIMATE_FIELDS)
    else:
        d = data
    return {"source_call_id": h["call_id"], "source_event_id": event_ids.get(h["call_id"]), "tool": tool,
            "arguments": h["arguments"], "result": {**pick(r, ("status", "code", "message", "retryable", "missing", "issues")), "data": d}}


def build_model_context(context):
    if "state" not in context:
        return context
    state, catalog = context["state"], context["catalog"]
    history, observations = state["history"], state["observations"]
    event_ids = {o["result"].get("meta", {}).get("call_id"): o["event_id"] for o in observations if "result" in o}
    materials = [pick(m, ("material_id", "kind", "title")) for m in state["materials"].values()]
    # Keep current scripts plus latest per option. Old versions cannot be saved.
    selected_scripts = {}
    for s in state["scripts"].values():
        selected_scripts[s["option_id"]] = s
    scripts = [pick(s, ("script_id", "option_id", "plan_version", "material_ids", "missing_sections"))
               for s in state["scripts"].values()
               if s["plan_version"] == state["plan_version"] or selected_scripts[s["option_id"]] is s]
    visible_scripts = {s["script_id"] for s in scripts}
    latest = {}
    for e in state["estimates"].values():
        if e["script_id"] is None or e["script_id"] in visible_scripts:
            latest[(e["option_id"], e["script_id"])] = e
    estimates = [pick(e, ESTIMATE_FIELDS) for e in latest.values()]
    observed_constraints, budget_evidence = {}, {}
    groups, failures, recent_errors, completed = {}, {}, {}, {}
    for h in history:
        r, args, tool = h["result"], h["arguments"], h["tool"]
        d = r.get("data") or {}
        key = signature(tool, args)
        group_key = (key, r["status"], r["code"])
        group = groups.pop(group_key, {"tool": tool, "arguments": args, "status": r["status"], "code": r["code"],
                                      "count": 0, "first_call_id": h["call_id"]})
        group.update(count=group["count"] + 1, last_call_id=h["call_id"])
        groups[group_key] = group
        if r["status"] in ("success", "partial"):
            completed[tool] = {"tool": tool, "status": "succeeded" if r["status"] == "success" else "partial", "step_id": h["call_id"]}
        if r["status"] != "success":
            failures[key] = result_view(h, event_ids)
            recent_errors.pop(tool, None)
            recent_errors[tool] = failures[key]
        else:
            # Only actual success with identical arguments resolves this scope.
            failures.pop(key, None)
        if tool == "estimate_plan" and r["status"] in ("success", "partial") and "estimate_id" in d:
            fact = {"source_call_id": h["call_id"], "source_event_id": event_ids.get(h["call_id"]),
                    "estimate_id": d["estimate_id"], "script_id": d["script_id"], "plan_version": d["plan_version"],
                    "quoted_cost": d["estimated_total_cost"], "budget_gap": d["budget_gap"], "time_gap_seconds": d["time_gap_seconds"],
                    "feasible": d["feasible"], "save_ready": d["save_ready"], "evaluated_at": d["evaluated_at"]}
            observed_constraints[d["option_id"]] = fact
            if d.get("budget_gap", 0):
                budget_evidence.setdefault(d["option_id"], fact)
    dependencies = []
    for option_id, opt in catalog["options"].items():
        active = [s for s in scripts if s["option_id"] == option_id and not s["missing_sections"] and s["plan_version"] == state["plan_version"]]
        ready = [e["estimate_id"] for e in estimates if e["option_id"] == option_id and e["save_ready"] and any(s["script_id"] == e["script_id"] for s in active)]
        missing_kinds = sorted(set(opt["required_kinds"]) - {m["kind"] for m in materials})
        dependencies.append({"option_id": option_id, "materials_complete": not missing_kinds, "missing_material_kinds": missing_kinds,
                             "missing_complete_script": not bool(active), "missing_matching_save_ready_estimate": not bool(ready), "save_ready_estimate_ids": ready})
    rejected = [o for o in observations if o.get("code") == "invalid_action"]
    errors = list(recent_errors.values())[-2:] + rejected[-1:]
    # Unresolved validation feedback stays even after unrelated tool successes.
    # Distinct essential errors are not silently dropped: the byte guard refuses
    # oversized views. Equal error details collapse to their latest occurrence.
    unique_rejected = {}
    for o in rejected:
        k = json.dumps(pick(o, ("issues", "rejected_action")), ensure_ascii=False, sort_keys=True)
        unique_rejected[k] = o
    recent = result_view(history[-1], event_ids) if history else None
    refs = {v.get("source_event_id") for v in [*observed_constraints.values(), *budget_evidence.values(),
                                              *failures.values(), *recent_errors.values(), *([recent] if recent else [])]}
    refs.update(o["event_id"] for o in unique_rejected.values())
    view = {"task": context["task"], "catalog": catalog, "current_time": context["current_time"], "remaining_rounds": context["remaining_rounds"],
            "state": {"status": state["status"], "plan_version": state["plan_version"], "completed_steps": list(completed.values()),
                      "available_materials": materials, "available_scripts": scripts, "available_estimates": estimates,
                      "material_reuse": {"available_count": len(materials), "all_catalog_options_materials_complete": all(d["materials_complete"] for d in dependencies)},
                      "observed_constraints_by_option": observed_constraints, "first_over_budget_observation_by_option": budget_evidence,
                      "available_ids": {"material_ids": [m["material_id"] for m in materials], "script_ids": [s["script_id"] for s in scripts], "estimate_ids": [e["estimate_id"] for e in estimates]},
                      "dependencies_by_option": dependencies, "recent_errors": errors,
                      "unresolved_tool_errors": list(failures.values()), "validation_error_evidence": list(unique_rejected.values())},
            "recent_tool_result": recent,
            "action_history_summary": {"total_tool_calls": len(history), "groups": list(groups.values())[-HISTORY_GROUPS:],
                                       "omitted_group_count": max(0, len(groups) - HISTORY_GROUPS),
                                       "archived_script_count": len(state["scripts"]) - len(scripts),
                                       "archived_estimate_count": len(state["estimates"]) - len(estimates)},
            "observation_refs": [{"event_id": o["event_id"], "tool": o.get("tool"), "code": o.get("code", o.get("result", {}).get("code"))}
                                 for o in observations if o["event_id"] in refs]}
    # Never truncate JSON or hide essential evidence to fit. Bytes are not tokens;
    # the frozen experiment separately audits server-reported output headroom.
    if len(json.dumps(view, ensure_ascii=False).encode("utf-8")) > MAX_VIEW_BYTES:
        from .model import ModelError
        raise ModelError("模型事实视图超过 16000 UTF-8 字节上限；未截断关键证据或发出请求")
    return copy.deepcopy(view)
