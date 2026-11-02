import hashlib
import json
import re
from datetime import datetime, timezone

DEFAULT_MAX_ROUNDS = 10


class ValidationError(ValueError):
    def __init__(self, message, issues=None):
        super().__init__(message)
        self.issues = issues or [{"path": "$", "code": "validation_error", "message": message}]


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def parse_time(value):
    try:
        if not isinstance(value, str) or "T" not in value:
            raise ValueError()
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise ValueError()
        return dt.astimezone(timezone.utc)
    except ValueError:
        raise ValidationError("deadline 必须为带时区的 ISO 8601 时间") from None


def normalize(raw):
    fields = ["task_id", "theme", "budget", "deadline"]
    if not isinstance(raw, dict) or set(raw) - set(fields + ["instructions"]):
        raise ValidationError("输入必须为对象，且只能包含 task_id/theme/budget/deadline 和可选 instructions")
    missing = [k for k in fields if raw.get(k) is None or raw.get(k) == ""]
    if missing:
        return None, missing
    if not isinstance(raw["task_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", raw["task_id"]):
        raise ValidationError("task_id 只能包含 1–64 位字母、数字、下划线或连字符")
    if not isinstance(raw["theme"], str) or not raw["theme"].strip():
        raise ValidationError("theme 必须是非空字符串")
    if type(raw["budget"]) is not int or raw["budget"] < 0:
        raise ValidationError("budget 必须是非负整数")
    task = dict(task_id=raw["task_id"], theme=raw["theme"].strip(), budget=raw["budget"],
                deadline=parse_time(raw["deadline"]).isoformat())
    if "instructions" in raw:
        if not isinstance(raw["instructions"], str) or not raw["instructions"].strip() or len(raw["instructions"]) > 1000:
            raise ValidationError("instructions 必须为 1–1000 字的非空任务说明")
        task["instructions"] = raw["instructions"].strip()
    return task, []


def fingerprint(task):
    return hashlib.sha256(dumps({k: v for k, v in task.items() if k != "task_id"}).encode()).hexdigest()


TOOL_SPECS = {
    "search_materials": {"required": {"query": "string", "required_kinds": "strings"}, "optional": {}},
    "generate_script": {"required": {"option_id": "string", "material_ids": "strings"}, "optional": {}},
    "estimate_plan": {"required": {"option_id": "string"}, "optional": {"script_id": "string"}},
    "save_plan": {"required": {"plan_version": "positive_int", "script_id": "string", "estimate_id": "string"}, "optional": {}},
}


TOOL_NOTES = {
    "search_materials": "检索合成素材，返回真实 material_id；已有足够素材时无需重复检索。",
    "generate_script": "用已有 material_ids 生成脚本；成功后才产生真实 script_id 和 plan_version。",
    "estimate_plan": "不传 script_id 是候选估算，永远不能直接保存。传已有真实 script_id 才能检查保存依赖；绝不能填 0、空串或虚构 ID。",
    "save_plan": "需要完整真实脚本、匹配当前版本且 save_ready=true 的 estimate_id；再次检查预算、时间和依赖后才保存。",
}


def object_schema(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def field_schema(kind):
    if kind == "strings":
        return {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "uniqueItems": True}
    if kind == "positive_int":
        return {"type": "integer", "minimum": 1}
    return {"type": "string", "minLength": 1}


SUMMARY_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 300}
ACTION_BRANCHES = {}
for name, spec in TOOL_SPECS.items():
    args = object_schema({k: field_schema(t) for k, t in {**spec["required"], **spec["optional"]}.items()}, list(spec["required"]))
    args["description"] = TOOL_NOTES[name]
    ACTION_BRANCHES[name] = object_schema({"type": {"type": "string", "enum": ["tool_call"]},
                                          "tool": {"type": "string", "enum": [name]},
                                          "arguments": args, "decision_summary": SUMMARY_SCHEMA},
                                         ["type", "tool", "arguments", "decision_summary"])
ACTION_BRANCHES["finish"] = object_schema({"type": {"type": "string", "enum": ["finish"]},
                                            "decision_summary": SUMMARY_SCHEMA}, ["type", "decision_summary"])
ACTION_SCHEMA = {"anyOf": list(ACTION_BRANCHES.values())}


def json_type(value):
    return ("null" if value is None else "boolean" if type(value) is bool else "integer" if type(value) is int
            else "number" if type(value) is float else "string" if isinstance(value, str)
            else "object" if isinstance(value, dict) else "array" if isinstance(value, list) else type(value).__name__)


def schema_issues(value, schema, path="$"):
    """Validate the small JSON Schema subset used by ACTION_SCHEMA, with precise paths."""
    issues = []
    def issue(code, expected, actual, message):
        issues.append(dict(path=path, code=code, expected=expected, actual=actual, message=message))
    expected = schema.get("type")
    if expected and json_type(value) != expected:
        issue("type_mismatch", expected, json_type(value), "字段类型不匹配")
        return issues
    if "enum" in schema and value not in schema["enum"]:
        issue("enum_mismatch", schema["enum"], json_type(value), "字段值不在允许范围")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                issues.append(dict(path=path + "." + key, code="missing_field", expected=schema["properties"][key].get("type"), actual="missing", message="缺失必填字段"))
        for key, child in value.items():
            if key not in schema.get("properties", {}):
                issues.append(dict(path=path + "." + key, code="extra_field", expected="absent", actual=json_type(child), message="不允许的多余字段"))
            else:
                issues.extend(schema_issues(child, schema["properties"][key], path + "." + key))
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            issue("too_few_items", schema["minItems"], len(value), "数组元素不足")
        if schema.get("uniqueItems") and len({dumps(v) for v in value}) != len(value):
            issue("duplicate_items", "unique", "duplicates", "数组元素必须去重")
        for i, child in enumerate(value):
            issues.extend(schema_issues(child, schema["items"], path + "[" + str(i) + "]"))
    elif isinstance(value, str):
        if not value.strip() or len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", float("inf")):
            issue("string_length", {"min": schema.get("minLength", 1), "max": schema.get("maxLength")}, len(value), "字符串为空白或长度超出限制")
    elif type(value) is int and value < schema.get("minimum", value):
        issue("below_minimum", schema["minimum"], value, "数值小于允许下限")
    return issues


def validate_action(action):
    if not isinstance(action, dict):
        issues = [dict(path="$", code="type_mismatch", expected="object", actual=json_type(action), message="动作必须为对象")]
    elif action.get("type") == "finish":
        issues = schema_issues(action, ACTION_BRANCHES["finish"])
    elif action.get("type") == "tool_call" and isinstance(action.get("tool"), str) and action["tool"] in TOOL_SPECS:
        issues = schema_issues(action, ACTION_BRANCHES[action["tool"]])
    else:
        generic = object_schema({"type": {"type": "string", "enum": ["tool_call", "finish"]},
                                 "tool": {"type": "string", "enum": list(TOOL_SPECS)},
                                 "arguments": {"type": "object", "properties": {}},
                                 "decision_summary": SUMMARY_SCHEMA}, ["type", "tool", "arguments", "decision_summary"])
        # Unknown tool/type: report discriminators without guessing a tool's argument schema.
        candidate = dict(action)
        if isinstance(candidate.get("arguments"), dict):
            candidate["arguments"] = {}
        issues = schema_issues(candidate, generic)
    if issues:
        raise ValidationError("; ".join(i["path"] + ": " + i["message"] for i in issues), issues)
    return action
