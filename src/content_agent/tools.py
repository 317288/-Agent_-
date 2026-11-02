"""Synthetic business tools. Fees are quotations, never accumulated charges."""
import copy
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from .schemas import ValidationError, parse_time


def uid(prefix):
    return prefix + "-" + uuid4().hex[:12]


def result(status="success", code="ok", message="执行成功", data=None, missing=None, issues=None, retryable=False):
    return dict(status=status, code=code, message=message, data=data, missing=missing or [], issues=issues or [], retryable=retryable)


class BusinessTools:
    def __init__(self, data_dir, clock, fail_once=None, partial_once=None, fault_task_id=None):
        base = Path(data_dir)
        self.catalog = json.loads((base / "plan_catalog.json").read_text(encoding="utf-8-sig"))
        self.material_catalog = json.loads((base / "materials.json").read_text(encoding="utf-8-sig"))
        if self.catalog["catalog_version"] != self.material_catalog["catalog_version"]:
            raise ValidationError("素材与方案目录版本不一致")
        self.clock, self.fail_once, self.partial_once = clock, fail_once, partial_once
        self.fault_task_id = fault_task_id
        self.fault_consumed = False

    def execute(self, name, args, state):
        if self.fault_task_id is None:
            self.fault_task_id = state["task"]["task_id"]
        if self.fail_once == name and not self.fault_consumed and state["task"]["task_id"] == self.fault_task_id:
            self.fault_consumed = True
            return result("error", "injected_failure", "测试注入：本任务首次相关调用发生临时错误；此前成功结果保留。由模型决定重试、改选或停止。", retryable=True,
                          issues=[{"code": "temporary_failure", "message": "仅当前任务首次相关调用生效；无底层自动重试", "task_id": self.fault_task_id, "tool": name, "injected": True}])
        try:
            return getattr(self, name)(state, **args)
        except (ValidationError, KeyError) as exc:
            # Dependency errors contain only our own fixed messages or resource IDs.
            return result("error", "dependency_error", str(exc), issues=getattr(exc, "issues", None))

    def option(self, option_id):
        if option_id not in self.catalog["options"]:
            raise ValidationError("未知方案 ID")
        return self.catalog["options"][option_id]

    def search_materials(self, state, query, required_kinds):
        allowed = {k for o in self.catalog["options"].values() for k in o["required_kinds"]}
        if not set(required_kinds) <= allowed:
            raise ValidationError("未知素材类别")
        found = [copy.deepcopy(m) for m in self.material_catalog["materials"]
                 if m["kind"] in required_kinds and any(t.casefold() in query.casefold() for t in m["tags"])]
        if self.partial_once == "search_materials":
            self.partial_once = None
            found = found[:1]
        covered = sorted({m["kind"] for m in found})
        missing = sorted(set(required_kinds) - set(covered))
        search_id = uid("search")
        for material in found:
            state["materials"][material["material_id"]] = material
        data = dict(search_id=search_id, materials=found, covered_kinds=covered, missing_kinds=missing)
        return result("partial" if missing else "success", "materials_missing" if missing else "ok",
                      "素材不足" if missing else "素材检索完成", data, missing)

    def generate_script(self, state, option_id, material_ids):
        opt = self.option(option_id)
        if not set(material_ids) <= set(state["materials"]):
            raise ValidationError("脚本只能引用本任务已检索素材")
        kinds = {state["materials"][mid]["kind"] for mid in material_ids}
        if not set(opt["required_kinds"]) <= kinds:
            raise ValidationError("素材未满足方案必需类别")
        theme = state["task"]["theme"]
        body = "；".join(state["materials"][mid]["content"] for mid in material_ids)
        texts = [f"{theme}：{opt['title']}，用校园记忆开启毕业故事。", body,
                 "邀请同学分享毕业寄语，投稿前确认肖像及内容授权；本项目仅输出计划，不实际发布。"]
        sections = [dict(name=name, text=texts[i % len(texts)], material_ids=material_ids)
                    for i, name in enumerate(opt["required_sections"])]
        missing = []
        if self.partial_once == "generate_script":
            self.partial_once = None
            missing = [sections.pop()["name"]]
        version = state["plan_version"] + 1
        script = dict(script_id=uid("script"), plan_version=version, option_id=option_id,
                      material_ids=material_ids, sections=sections, required_sections=opt["required_sections"],
                      missing_sections=missing, generation_mode="template")
        state["plan_version"] = version
        state["scripts"][script["script_id"]] = script
        return result("partial" if missing else "success", "script_incomplete" if missing else "ok",
                      "模板脚本部分生成" if missing else "模板脚本生成完成", script, missing)

    def quote(self, task, option_id, now=None):
        opt = self.option(option_id)
        now = now or self.clock()
        items = copy.deepcopy(opt["items"])
        seen, missing = set(), []
        cost, minutes = 0, 0
        for row in items:
            if row["item_id"] in seen or not set(row["depends_on"]) <= seen:
                raise ValidationError("活动依赖缺失、循环或非串行顺序")
            seen.add(row["item_id"])
            for key in ("quantity", "unit_cost", "duration_minutes"):
                if type(row.get(key)) is not int or row[key] < (1 if key == "quantity" else 0):
                    missing.append(row["item_id"] + "." + key)
            if any(x.startswith(row["item_id"] + ".") for x in missing):
                row["subtotal"] = None
                continue
            row["subtotal"] = row["quantity"] * row["unit_cost"]
            cost += row["subtotal"]
            minutes += row["quantity"] * row["duration_minutes"]
        if not items:
            missing.append("items")
        finish = None if missing else now + timedelta(minutes=minutes)
        budget_gap = None if missing else max(0, cost - task["budget"])
        time_gap = None if missing else max(0, (finish - parse_time(task["deadline"])).total_seconds())
        return dict(option_id=option_id, catalog_version=self.catalog["catalog_version"], evaluated_at=now.isoformat(),
                    line_items=items, estimated_total_cost=None if missing else cost,
                    duration_minutes=None if missing else minutes, projected_finish_at=finish.isoformat() if finish else None,
                    budget_gap=budget_gap, time_gap_seconds=time_gap, missing=missing,
                    checks={"budget_ok": None if missing else budget_gap == 0,
                            "time_ok": None if missing else time_gap == 0, "dependencies_ok": False},
                    feasible=not missing and budget_gap == 0 and time_gap == 0, save_ready=False)

    def script_ready(self, state, script):
        opt = self.option(script["option_id"])
        kinds = {state["materials"][mid]["kind"] for mid in script["material_ids"] if mid in state["materials"]}
        return (script["plan_version"] == state["plan_version"] and not script["missing_sections"]
                and set(opt["required_sections"]) <= {s["name"] for s in script["sections"] if s["text"].strip()}
                and set(opt["required_kinds"]) <= kinds)

    def estimate_plan(self, state, option_id, script_id=None):
        script = state["scripts"].get(script_id)
        if script_id and (script is None or script["option_id"] != option_id):
            raise ValidationError("脚本引用不属于本任务/方案；候选估算请省略 script_id", [
                {"path": "$.arguments.script_id", "code": "unknown_or_mismatched_id", "expected": "已有且属于该 option_id 的 script_id；无脚本时省略该字段",
                 "actual": script_id, "available_script_ids": list(state["scripts"]), "message": "不能使用虚构脚本 ID"}])
        quote = self.quote(state["task"], option_id)
        quote.update(estimate_id=uid("estimate"), script_id=script_id, plan_version=script["plan_version"] if script else None)
        quote["checks"]["dependencies_ok"] = bool(script and self.script_ready(state, script))
        quote["save_ready"] = quote["feasible"] and quote["checks"]["dependencies_ok"]
        state["estimates"][quote["estimate_id"]] = quote
        issues = [{"code": name, "message": str(quote[name])} for name in ("budget_gap", "time_gap_seconds") if quote[name]]
        return result("partial" if quote["missing"] else "success", "estimate_incomplete" if quote["missing"] else "ok",
                      "估算完成（可行性见 feasible，费用不累计）", quote, quote["missing"], issues)

    def save_plan(self, state, plan_version, script_id, estimate_id):
        script, estimate = state["scripts"].get(script_id), state["estimates"].get(estimate_id)
        if not script or not estimate or plan_version != state["plan_version"] or script["plan_version"] != plan_version:
            raise ValidationError("缺少产物或方案版本已过期")
        if estimate["script_id"] != script_id or estimate["plan_version"] != plan_version or estimate["catalog_version"] != self.catalog["catalog_version"]:
            raise ValidationError("估算未绑定当前脚本/方案版本/目录版本")
        if not estimate["save_ready"] or not self.script_ready(state, script):
            raise ValidationError("脚本、估算或依赖未满足保存条件")
        current = self.quote(state["task"], script["option_id"])
        if not current["feasible"]:
            return result("error", "constraints_failed", "保存时预算或时间检查未通过", issues=[
                {"code": "constraints", "message": "重新计算结果", "budget_gap": current["budget_gap"], "time_gap_seconds": current["time_gap_seconds"]}])
        if current["line_items"] != estimate["line_items"]:
            raise ValidationError("目录内容与估算不一致，需重新估算")
        current["checks"]["dependencies_ok"] = True
        current["save_ready"] = True
        current.update(script_id=script_id, plan_version=plan_version, source_estimate_id=estimate_id)
        plan_id = uid("plan")
        plan = dict(saved_plan_id=plan_id, task=state["task"], plan_version=plan_version,
                    option_id=script["option_id"], script=script, estimate=current,
                    activity_steps=[{**row, "status": "planned"} for row in current["line_items"]],
                    saved_at=self.clock().isoformat())
        # Commit is owned by the agent: saved artifact + result + task status are one transaction.
        state["saved_plan"] = plan
        return result(data=dict(saved_plan_id=plan_id, plan_version=plan_version,
                                saved_plan_ref=state["database_ref"] + "#plan/" + plan_id,
                                estimated_total_cost=current["estimated_total_cost"], duration_minutes=current["duration_minutes"],
                                projected_finish_at=current["projected_finish_at"], saved_at=plan["saved_at"], already_saved=False))
