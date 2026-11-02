import copy
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from content_agent.agent import Agent, Trace
from content_agent.model import LiveModel, MockModel, ModelError, InvalidModelOutput, call
from content_agent.schemas import ValidationError, normalize, parse_time, validate_action
from content_agent.state import Store
from content_agent.tools import BusinessTools


NOW = parse_time("2026-11-01T12:00:00+08:00")


class ScriptedModel:
    """Explicit test double for adversarial actions, not a real model."""
    def __init__(self, actions):
        self.actions = iter(actions)

    def decide(self, context):
        action = next(self.actions)
        return action(context) if callable(action) else action


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = Store(self.base / "tasks.sqlite3")
        self.task = dict(task_id="test-campus", theme="校园毕业季", budget=12, deadline="2026-11-01T18:00:00+08:00")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_agent(self, task=None, model_factory=MockModel, rounds=8, fail=None, partial=None, tools=None, clock=lambda: NOW):
        run_id = uuid4().hex
        trace = Trace(self.base / (run_id + ".jsonl"), "mock", "test-double", run_id)
        tools = tools or BusinessTools(ROOT / "data", clock, fail, partial)
        try:
            out = Agent(self.store, tools, model_factory, "mock", "test-double", trace, rounds, clock, True).run(task or self.task)
        finally:
            trace.close()
        self.events = [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]
        return out

    def test_normal_complete_and_observation_feedback(self):
        out = self.run_agent()
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["result"]["estimated_total_cost"], 12)
        plan = self.store.get_plan(self.task["task_id"])
        self.assertTrue(plan["estimate"]["save_ready"])
        self.assertTrue(all(plan["estimate"]["checks"].values()))
        self.assertEqual(plan["estimate"]["duration_minutes"], 120)
        self.assertTrue(all(x["status"] == "planned" for x in plan["activity_steps"]))
        actions = [e for e in self.events if e["event_type"] == "model_action"]
        results = [e for e in self.events if e["event_type"] == "tool_result"]
        self.assertIn(results[0]["event_id"], actions[1]["payload"]["observed_event_ids"])

    def test_budget_switch_is_mock_only(self):
        self.task["budget"] = 10
        out = self.run_agent()
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["result"]["estimated_total_cost"], 8)
        estimates = [h["result"]["data"] for h in out["result"]["tool_results"] if h["tool"] == "estimate_plan"]
        self.assertEqual([e["estimated_total_cost"] for e in estimates], [12, 8, 8])
        self.assertFalse(estimates[0]["feasible"])

    def test_missing_fields_no_task_claim(self):
        for fields in (["budget"], ["deadline"], ["budget", "deadline"]):
            raw = {k: v for k, v in self.task.items() if k not in fields}
            out = self.run_agent(raw)
            self.assertEqual(out["status"], "needs_input")
            self.assertEqual(out["missing_fields"], fields)
            self.assertIsNone(self.store.get(raw["task_id"]))
        self.assertEqual(self.run_agent()["status"], "completed")

    def test_failure_retains_materials_and_does_not_lie(self):
        out = self.run_agent(fail="generate_script")
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["result"]["estimated_total_cost"], 8)
        self.assertEqual(len(out["result"]["artifacts"]["materials"]), 2)
        self.assertEqual(sum(s["status"] == "failed" for s in out["result"]["steps"]), 1)
        self.assertEqual(sum(s["tool"] == "search_materials" for s in out["result"]["steps"]), 1)

    def test_no_feasible_budget_reports_both_options(self):
        self.task["budget"] = 5
        out = self.run_agent()
        self.assertEqual(out["status"], "incomplete")
        self.assertEqual(out["result"]["incomplete_reason"]["code"], "no_feasible_plan")
        self.assertEqual([q["budget_gap"] for q in out["result"]["gaps_by_option"]], [7, 3])
        self.assertIsNone(self.store.get_plan(self.task["task_id"]))

    def test_time_limit_and_exact_boundary(self):
        self.task["deadline"] = "2026-11-01T12:30:00+08:00"
        out = self.run_agent()
        self.assertEqual(out["status"], "incomplete")
        self.assertEqual([q["time_gap_seconds"] for q in out["result"]["gaps_by_option"]], [5400, 1800])
        self.task.update(task_id="boundary", deadline="2026-11-01T13:00:00+08:00")
        out = self.run_agent()
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["result"]["estimated_total_cost"], 8)

    def test_expired_deadline(self):
        self.task["deadline"] = "2026-11-01T11:59:00+08:00"
        out = self.run_agent()
        self.assertEqual(out["result"]["incomplete_reason"]["code"], "deadline_passed")
        self.assertEqual(out["result"]["incomplete_reason"]["overdue_seconds"], 60)
        self.assertEqual(out["result"]["steps"], [])

    def test_replay_after_deadline_and_timezone_normalization(self):
        first = self.run_agent()
        self.task["deadline"] = "2026-11-01T10:00:00Z"
        out = self.run_agent(clock=lambda: parse_time("2027-01-01T00:00:00Z"), model_factory=lambda: self.fail("Must not call model"))
        self.assertTrue(out["replayed"])
        self.assertEqual(first["result"], out["result"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM plans").fetchone()[0], 1)
        self.assertFalse(any(e["event_type"] in ("model_action", "tool_result") for e in self.events))

    def test_input_conflict_preserves_original(self):
        first = self.run_agent()
        self.task["budget"] = 99
        out = self.run_agent()
        self.assertEqual(out["error"]["code"], "task_id_conflict")
        self.assertEqual(self.store.get_plan(self.task["task_id"]), first["result"]["plan"])

    def test_incomplete_does_not_resume(self):
        self.run_agent(rounds=1)
        out = self.run_agent(model_factory=lambda: self.fail("Must not resume"))
        self.assertEqual(out["status"], "incomplete")
        self.assertTrue(out["replayed"])

    def test_finish_cannot_claim_completed(self):
        out = self.run_agent(model_factory=lambda: ScriptedModel([{"type": "finish", "decision_summary": "我已完成所有工作"}]))
        self.assertEqual(out["status"], "incomplete")
        self.assertIsNone(out["result"]["saved_plan_ref"])
        self.assertIsNone(self.store.get_plan(self.task["task_id"]))

    def test_invalid_actions_are_bounded_and_not_logged_raw(self):
        bad = {"type": "tool_call", "tool": "shell", "arguments": {}, "decision_summary": "运行", "reasoning_content": "do-not-store"}
        out = self.run_agent(model_factory=lambda: ScriptedModel([bad] * 3), rounds=3)
        self.assertEqual(out["result"]["incomplete_reason"]["code"], "step_limit")
        self.assertEqual(len([e for e in self.events if e["event_type"] == "action_rejected"]), 3)
        self.assertNotIn("do-not-store", json.dumps(self.events))

    def test_save_missing_dependency_is_error(self):
        actions = [call("save_plan", {"plan_version": 1, "script_id": "fake", "estimate_id": "fake"}, "测试非法引用")]
        out = self.run_agent(model_factory=lambda: ScriptedModel(actions), rounds=1)
        self.assertEqual(out["result"]["steps"][0]["status"], "failed")
        self.assertIsNone(self.store.get_plan(self.task["task_id"]))

    def test_partial_script_cannot_be_saved(self):
        out = self.run_agent(partial="generate_script", rounds=3)
        self.assertEqual(out["result"]["steps"][-1]["status"], "partial")
        state = self.store.get(self.task["task_id"])
        tools = BusinessTools(ROOT / "data", lambda: NOW)
        script = next(iter(state["scripts"].values()))
        estimate = tools.estimate_plan(state, script["option_id"], script["script_id"])["data"]
        self.assertFalse(estimate["save_ready"])
        saved = tools.execute("save_plan", dict(plan_version=script["plan_version"], script_id=script["script_id"], estimate_id=estimate["estimate_id"]), state)
        self.assertEqual(saved["status"], "error")

    def test_empty_search_is_partial(self):
        self.task["theme"] = "不存在的主题"
        out = self.run_agent()
        self.assertEqual(out["status"], "incomplete")
        self.assertEqual(out["result"]["tool_results"][0]["result"]["status"], "partial")

    def test_unknown_price_is_not_zero(self):
        tools = BusinessTools(ROOT / "data", lambda: NOW)
        tools.catalog["options"]["graduation_video"]["items"][0].pop("unit_cost")
        out = self.run_agent(tools=tools)
        first = next(h for h in out["result"]["tool_results"] if h["tool"] == "estimate_plan")
        self.assertEqual(first["result"]["status"], "partial")
        self.assertIsNone(first["result"]["data"]["estimated_total_cost"])
        self.assertEqual(out["result"]["estimated_total_cost"], 8)

    def prepared(self):
        self.run_agent(rounds=4)
        state = self.store.get(self.task["task_id"])
        script = next(iter(state["scripts"].values()))
        estimate = next(e for e in state["estimates"].values() if e["script_id"])
        return state, script, estimate

    def test_stale_estimate_rejected(self):
        state, script, estimate = self.prepared()
        tools = BusinessTools(ROOT / "data", lambda: NOW)
        tools.generate_script(state, "graduation_slides", list(state["materials"]))
        saved = tools.execute("save_plan", dict(plan_version=script["plan_version"], script_id=script["script_id"], estimate_id=estimate["estimate_id"]), state)
        self.assertEqual(saved["status"], "error")

    def test_save_rechecks_clock(self):
        state, script, estimate = self.prepared()
        tools = BusinessTools(ROOT / "data", lambda: parse_time("2026-11-01T17:00:00+08:00"))
        saved = tools.execute("save_plan", dict(plan_version=script["plan_version"], script_id=script["script_id"], estimate_id=estimate["estimate_id"]), state)
        self.assertEqual(saved["status"], "error")
        self.assertEqual(saved["code"], "constraints_failed")
        self.assertIsNone(state["saved_plan"])

    def test_atomic_save_failure_does_not_complete(self):
        persist = self.store.persist
        def failing(state, plan=None):
            if plan is not None:
                raise sqlite3.OperationalError("test failure")
            return persist(state)
        with patch.object(self.store, "persist", side_effect=failing):
            out = self.run_agent(rounds=5)
        self.assertEqual(out["status"], "incomplete")
        self.assertEqual(out["result"]["steps"][-1]["status"], "failed")
        self.assertIsNone(out["result"]["plan"])
        self.assertIsNone(self.store.get_plan(self.task["task_id"]))

    def test_real_database_transaction_rolls_back_plan_insert(self):
        self.run_agent(rounds=1)
        state = self.store.get(self.task["task_id"])
        self.store.db.execute("CREATE TRIGGER reject_update BEFORE UPDATE ON tasks BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(sqlite3.Error):
            self.store.persist(state, {"saved_plan_id": "test-plan"})
        self.assertIsNone(self.store.get_plan(self.task["task_id"]))

    def test_schema_rejects_bool_budget_naive_time_and_unknown_args(self):
        for changes in ({"budget": True}, {"deadline": "2026-11-01T18:00:00"}, {"task_id": "../escape"}):
            with self.assertRaises(ValidationError):
                normalize({**self.task, **changes})
        with self.assertRaises(ValidationError):
            validate_action(call("estimate_plan", {"option_id": "graduation_video", "cost": 0}, "测试"))

    def test_live_missing_credentials_no_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            out = self.run_agent(model_factory=LiveModel)
        self.assertEqual(out["result"]["incomplete_reason"]["code"], "model_unavailable")
        self.assertEqual(out["result"]["steps"], [])

    def test_local_ollama_no_key_no_authorization_header(self):
        envelope = {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"type": "finish", "decision_summary": "停止"})}}]}
        for host in ("localhost", "127.0.0.1", "[::1]"):
            env = dict(AGENT_BASE_URL="http://" + host + ":11434/v1", AGENT_MODEL="qwen2.5:7b-instruct-q4_K_M")
            with self.subTest(host=host), patch.dict(os.environ, env, clear=True), patch("urllib.request.build_opener") as opener:
                opener.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps(envelope).encode()
                model = LiveModel()
                model.decide({"task": self.task})
                req = opener.return_value.open.call_args.args[0]
                self.assertIsNone(req.get_header("Authorization"))
                self.assertEqual(model.timeout, 180)
                self.assertEqual(json.loads(req.data)["model"], env["AGENT_MODEL"])

    def test_remote_transport_and_timeout_validation(self):
        for base, key, timeout in (("http://example.invalid/v1", "test", "45"),
                                   ("https://example.invalid/v1", "", "45"),
                                   ("http://localhost:11434/v1", "", "0")):
            with patch.dict(os.environ, dict(AGENT_BASE_URL=base, AGENT_MODEL="test", AGENT_API_KEY=key, AGENT_TIMEOUT_SECONDS=timeout), clear=True):
                with self.assertRaises(ModelError):
                    LiveModel()

    def test_live_adapter_transport_stub_excludes_reasoning(self):
        content = {"type": "finish", "decision_summary": "停止"}
        envelope = {"id": "stub-response", "model": "stub-model", "choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(content), "reasoning_content": "not-retained"}}]}
        env = dict(AGENT_API_KEY="test-placeholder", AGENT_BASE_URL="https://example.invalid/v1", AGENT_MODEL="test")
        with patch.dict(os.environ, env), patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps(envelope).encode()
            model = LiveModel()
            self.assertEqual(model.decide({"task": self.task}), content)
            self.assertNotIn("not-retained", json.dumps(model.last_metadata))
            req = opener.return_value.open.call_args.args[0]
            self.assertEqual(req.full_url, "https://example.invalid/v1/chat/completions")
            self.assertEqual(json.loads(req.data)["response_format"], {"type": "json_object"})

    def test_second_live_http_body_contains_first_actual_tool_result(self):
        env = dict(AGENT_BASE_URL="http://localhost:11434/v1", AGENT_MODEL="qwen2.5:7b-instruct-q4_K_M")
        actions = [call("search_materials", {"query": "校园毕业季", "required_kinds": ["campus_photo", "graduate_message"]}, "检索素材"),
                   {"type": "finish", "decision_summary": "结束传输测试"}]
        with patch.dict(os.environ, env, clear=True), patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.side_effect = [
                json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(action)}}]}).encode() for action in actions]
            self.run_agent(model_factory=LiveModel)
            sent = opener.return_value.open.call_args_list[1].args[0].data
        second_context = json.loads(json.loads(sent)["messages"][1]["content"])
        first = next(e for e in self.events if e["event_type"] == "tool_result")
        recent = second_context["recent_tool_result"]
        self.assertEqual(recent["source_call_id"], first["payload"]["call_id"])
        self.assertEqual(recent["source_event_id"], first["event_id"])
        self.assertEqual(recent["result"]["status"], first["payload"]["result"]["status"])
        self.assertEqual([m["material_id"] for m in recent["result"]["data"]["materials"]],
                         [m["material_id"] for m in first["payload"]["result"]["data"]["materials"]])
        self.assertNotIn("tool_history", second_context)
        self.assertEqual(second_context["state"]["available_ids"]["material_ids"], ["campus-photo-01", "graduate-message-01"])
        self.assertEqual(second_context["observation_refs"][0]["event_id"], first["event_id"])
        self.assertNotIn("history", second_context["state"])
        self.assertEqual((self.base / "model-requests" / "02.json").read_bytes(), sent)
        self.assertNotIn(b'Authorization', sent)

    def test_validation_paths_and_types_and_unchanged_action(self):
        bad = {"type": "tool_call", "tool": "save_plan", "arguments": {"plan_version": True, "script_id": 0, "extra": "x"}, "extra": 1}
        original = copy.deepcopy(bad)
        with self.assertRaises(ValidationError) as caught:
            validate_action(bad)
        issues = {i["path"]: i for i in caught.exception.issues}
        self.assertEqual(issues["$.decision_summary"]["actual"], "missing")
        self.assertEqual(issues["$.arguments.estimate_id"]["code"], "missing_field")
        self.assertEqual(issues["$.arguments.plan_version"]["expected"], "integer")
        self.assertEqual(issues["$.arguments.plan_version"]["actual"], "boolean")
        self.assertEqual(issues["$.arguments.script_id"]["actual"], "integer")
        self.assertEqual(issues["$.arguments.extra"]["code"], "extra_field")
        self.assertEqual(issues["$.extra"]["code"], "extra_field")
        self.assertEqual(bad, original)

    def test_rejected_action_and_details_reach_next_model(self):
        bad = {"type": "tool_call", "tool": "estimate_plan", "arguments": {}, "decision_summary": "估算"}
        def corrected(context):
            from content_agent.context import build_model_context
            compact = build_model_context(context)
            error = compact["state"]["recent_errors"][-1]
            self.assertEqual(error["rejected_action"], bad)
            self.assertEqual(error["issues"][0]["path"], "$.arguments.option_id")
            return {"type": "finish", "decision_summary": "停止测试"}
        out = self.run_agent(model_factory=lambda: ScriptedModel([bad, corrected]), rounds=2)
        self.assertEqual(out["result"]["steps"], [])
        self.assertEqual(len([e for e in self.events if e["event_type"] == "model_output"]), 2)

    def test_candidate_quote_and_zero_id_diagnostics(self):
        self.run_agent(rounds=1)
        state = self.store.get(self.task["task_id"])
        tools = BusinessTools(ROOT / "data", lambda: NOW)
        candidate = tools.estimate_plan(state, "graduation_video")["data"]
        self.assertTrue(candidate["feasible"])
        self.assertFalse(candidate["save_ready"])
        self.assertIsNone(candidate["script_id"])
        invalid = tools.execute("estimate_plan", {"option_id": "graduation_video", "script_id": "0"}, state)
        self.assertEqual(invalid["status"], "error")
        self.assertEqual(invalid["issues"][0]["path"], "$.arguments.script_id")
        self.assertEqual(invalid["issues"][0]["available_script_ids"], [])
        self.assertEqual(state["scripts"], {})

    def test_context_exposes_missing_dependencies_without_executing(self):
        from content_agent.context import build_model_context
        self.run_agent(rounds=2)
        state = self.store.get(self.task["task_id"])
        before = copy.deepcopy(state)
        tools = BusinessTools(ROOT / "data", lambda: NOW)
        compact = build_model_context({"state": state, "task": state["task"], "catalog": tools.catalog, "current_time": NOW.isoformat(), "remaining_rounds": 6})
        dep = compact["state"]["dependencies_by_option"][0]
        self.assertEqual(dep["missing_material_kinds"], [])
        self.assertTrue(dep["missing_complete_script"])
        self.assertTrue(dep["missing_matching_save_ready_estimate"])
        self.assertEqual(compact["state"]["available_ids"]["script_ids"], [])
        self.assertEqual(state, before)

    def test_public_content_parse_failure_recorded_and_bounded(self):
        env = dict(AGENT_BASE_URL="http://localhost:11434/v1", AGENT_MODEL="test")
        text = '{"type":"tool_call",'
        envelope = {"choices": [{"finish_reason": "stop", "message": {"content": text, "reasoning_content": "never-persist"}}]}
        with patch.dict(os.environ, env, clear=True), patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps(envelope).encode()
            out = self.run_agent(model_factory=LiveModel, rounds=2)
        outputs = [e["payload"] for e in self.events if e["event_type"] == "model_output"]
        self.assertEqual(outputs[0]["content"], text)
        self.assertFalse(outputs[0]["parse_ok"])
        self.assertEqual(outputs[0]["parse_issues"][0]["line"], 1)
        self.assertNotIn("never-persist", json.dumps(self.events))
        self.assertEqual(out["result"]["incomplete_reason"]["code"], "step_limit")
        sent = json.loads((self.base / "model-requests" / "02.json").read_text(encoding="utf-8"))
        feedback = json.loads(sent["messages"][1]["content"])["state"]["recent_errors"][-1]
        self.assertEqual(feedback["rejected_action"], text)

    def test_schema_request_shares_validator_contract(self):
        from content_agent.schemas import ACTION_SCHEMA
        env = dict(AGENT_BASE_URL="http://localhost:11434/v1", AGENT_MODEL="test")
        envelope = {"choices": [{"finish_reason": "stop", "message": {"content": '{"type":"finish","decision_summary":"停止"}'}}]}
        with patch.dict(os.environ, env, clear=True), patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps(envelope).encode()
            LiveModel().decide({"task": self.task})
            body = json.loads(opener.return_value.open.call_args.args[0].data)
        self.assertEqual(body["response_format"]["json_schema"]["schema"], ACTION_SCHEMA)
        self.assertEqual(json.loads(body["messages"][1]["content"])["action_schema"], ACTION_SCHEMA)
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(list(body["response_format"]["json_schema"]["schema"]["anyOf"][0]["properties"]),
                         ["type", "tool", "arguments", "decision_summary"])
        self.assertEqual(list(body["response_format"]["json_schema"]["schema"]["anyOf"][-1]["properties"]),
                         ["type", "decision_summary"])

    def test_private_fields_and_key_are_redacted_in_public_diagnostics(self):
        from content_agent.diagnostics import public_content
        text = '{"type":"finish","decision_summary":"local-test-secret","reasoning_content":"never-store"}'
        safe = public_content(text, ("local-test-secret",))
        self.assertTrue(safe["redacted"])
        self.assertNotIn("never-store", safe["content"])
        self.assertNotIn("local-test-secret", safe["content"])

    def test_native_ollama_schema_context_and_public_response(self):
        from content_agent.schemas import ACTION_SCHEMA
        env = dict(AGENT_BASE_URL="http://localhost:11434/v1", AGENT_MODEL="test", AGENT_PROTOCOL="ollama", AGENT_NUM_CTX="8192")
        envelope = {"model": "test", "done": True, "done_reason": "stop", "prompt_eval_count": 500,
                    "message": {"content": '{"type":"finish","decision_summary":"停止"}', "thinking": "do-not-save"}}
        with patch.dict(os.environ, env, clear=True), patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps(envelope).encode()
            model = LiveModel()
            action = model.decide({"task": self.task})
            req = opener.return_value.open.call_args.args[0]
            body = json.loads(req.data)
        self.assertEqual(req.full_url, "http://localhost:11434/api/chat")
        self.assertEqual(body["format"], ACTION_SCHEMA)
        self.assertEqual(body["options"]["num_ctx"], 8192)
        self.assertFalse(body["stream"])
        self.assertEqual(action["type"], "finish")
        self.assertEqual(model.last_metadata["usage"]["prompt_tokens"], 500)
        self.assertNotIn("do-not-save", json.dumps(model.last_public_output))

    def test_instructions_are_visible_and_part_of_conflict_identity(self):
        from content_agent.schemas import fingerprint
        task, _ = normalize({**self.task, "instructions": "优先评估视频，预算不足接受图文"})
        plain, _ = normalize(self.task)
        self.assertNotEqual(fingerprint(task), fingerprint(plain))
        def check(context):
            from content_agent.context import build_model_context
            self.assertEqual(build_model_context(context)["task"]["instructions"], task["instructions"])
            return {"type": "finish", "decision_summary": "仅测试偏好传递"}
        self.run_agent(task, model_factory=lambda: ScriptedModel([check]))
        conflict = self.run_agent({**task, "instructions": "只接受图文"})
        self.assertEqual(conflict["error"]["code"], "task_id_conflict")
        with self.assertRaises(ValidationError):
            normalize({**self.task, "instructions": ["not a string"]})

    def test_material_completeness_is_explicit_without_next_tool_selection(self):
        from content_agent.context import build_model_context
        self.run_agent(rounds=1)
        state = self.store.get(self.task["task_id"])
        before = copy.deepcopy(state)
        tools = BusinessTools(ROOT / "data", lambda: NOW)
        compact = build_model_context({"task": state["task"], "state": state, "catalog": tools.catalog, "current_time": NOW.isoformat(), "remaining_rounds": 9})
        self.assertTrue(compact["state"]["material_reuse"]["all_catalog_options_materials_complete"])
        self.assertTrue(all(d["materials_complete"] for d in compact["state"]["dependencies_by_option"]))
        self.assertEqual(compact["state"]["material_reuse"]["available_count"], 2)
        self.assertNotIn("next_tool", compact["state"])
        self.assertEqual(state, before)

    def test_failure_is_task_scoped_once_and_preserves_existing_script(self):
        state, script, estimate = self.prepared()
        tools = BusinessTools(ROOT / "data", lambda: NOW, fail_once="generate_script", fault_task_id=self.task["task_id"])
        other = copy.deepcopy(state)
        other["task"]["task_id"] = "unrelated"
        args = {"option_id": "graduation_video", "material_ids": list(state["materials"])}
        self.assertEqual(tools.execute("generate_script", args, other)["status"], "success")
        self.assertFalse(tools.fault_consumed)
        before = copy.deepcopy(state)
        failed = tools.execute("generate_script", args, state)
        self.assertEqual(failed["status"], "error")
        self.assertTrue(failed["retryable"])
        self.assertEqual(failed["issues"][0]["task_id"], self.task["task_id"])
        self.assertEqual(state, before)
        self.assertEqual(tools.execute("generate_script", args, state)["status"], "success")
        self.assertEqual(state["scripts"][script["script_id"]], script)
        self.assertEqual(state["estimates"][estimate["estimate_id"]], estimate)

    def test_quotes_twelve_eight_not_added_and_gap_two(self):
        tools = BusinessTools(ROOT / "data", lambda: NOW)
        task = {**self.task, "budget": 10}
        self.assertEqual(tools.quote(task, "graduation_video")["budget_gap"], 2)
        self.assertEqual(tools.quote(task, "graduation_slides")["estimated_total_cost"], 8)
        task["budget"] = 6
        quotes = [tools.quote(task, name) for name in tools.catalog["options"]]
        self.assertEqual(min(q["estimated_total_cost"] for q in quotes), 8)
        self.assertEqual(min(q["budget_gap"] for q in quotes), 2)
        self.assertTrue(all(not q["feasible"] for q in quotes))

    def test_save_rechecks_budget_even_if_estimate_was_previously_feasible(self):
        state, script, estimate = self.prepared()
        state["task"]["budget"] = 10
        tools = BusinessTools(ROOT / "data", lambda: NOW)
        saved = tools.execute("save_plan", {"plan_version": script["plan_version"], "script_id": script["script_id"], "estimate_id": estimate["estimate_id"]}, state)
        self.assertEqual(saved["status"], "error")
        self.assertEqual(saved["issues"][0]["budget_gap"], 2)
        self.assertIsNone(state["saved_plan"])

    def test_limit_does_not_become_no_feasible_plan(self):
        self.task["budget"] = 6
        out = self.run_agent(rounds=1)
        self.assertEqual(out["result"]["incomplete_reason"]["code"], "step_limit")
        self.assertEqual(out["result"]["incomplete_reason"]["feasibility_diagnosis"], "no_feasible_plan")
        self.assertIsNone(self.store.get_plan(self.task["task_id"]))

    def test_stage4_direct_feasible_selection_not_counted_as_replanning(self):
        from scripts.verify_stage4 import evaluate
        tools = BusinessTools(ROOT / "data", lambda: NOW)
        opts = tools.catalog["options"]
        tools.catalog["options"] = {"graduation_slides": opts["graduation_slides"], "graduation_video": opts["graduation_video"]}
        self.task["budget"] = 10
        out = self.run_agent(tools=tools)
        report = evaluate("A", out, self.events, self.store.get_plan(self.task["task_id"]), self.store.get(self.task["task_id"]))
        self.assertTrue(report["criteria"]["feasible_plan_completed"])
        self.assertTrue(report["criteria"]["direct_slides_without_over_budget_is_not_replanning"])
        self.assertFalse(report["scenario_passed"])

    def test_stage4_model_stop_after_failure_is_handled_not_recovered(self):
        from scripts.verify_stage4 import evaluate
        actions = [call("search_materials", {"query": "校园毕业季", "required_kinds": ["campus_photo", "graduate_message"]}, "检索"),
                   lambda c: call("generate_script", {"option_id": "graduation_slides", "material_ids": list(c["state"]["materials"])}, "生成"),
                   {"type": "finish", "decision_summary": "工具失败后决定停止"}]
        out = self.run_agent(model_factory=lambda: ScriptedModel(actions), fail="generate_script")
        report = evaluate("B", out, self.events, None, self.store.get(self.task["task_id"]))
        self.assertTrue(report["criteria"]["program_handled_failure_correctly"])
        self.assertFalse(report["criteria"]["model_recovered_task"])
        self.assertFalse(report["scenario_passed"])

    def test_stage4_limit_is_not_verified_no_feasible_exit(self):
        from scripts.verify_stage4 import evaluate
        self.task["budget"] = 6
        out = self.run_agent(rounds=1)
        report = evaluate("C", out, self.events, None, self.store.get(self.task["task_id"]))
        self.assertFalse(report["criteria"]["not_step_limit"])
        self.assertFalse(report["criteria"]["program_returned_verified_no_feasible_plan"])
        self.assertFalse(report["scenario_passed"])

    def compact_state(self, state):
        from content_agent.context import build_model_context
        return build_model_context({"task": state["task"], "state": state,
                                    "catalog": BusinessTools(ROOT / "data", lambda: NOW).catalog,
                                    "current_time": NOW.isoformat(), "remaining_rounds": 4})

    def test_context_long_history_keeps_budget_evidence_and_is_bounded(self):
        self.task["budget"] = 10
        self.run_agent(rounds=2)
        state = self.store.get(self.task["task_id"])
        initial = self.compact_state(state)
        first = initial["state"]["first_over_budget_observation_by_option"]["graduation_video"]
        h = copy.deepcopy(state["history"][-1])
        o = copy.deepcopy(state["observations"][-1])
        for i in range(2000):
            call_id, event_id, estimate_id = 'repeat-call-' + str(i), 'repeat-event-' + str(i), 'repeat-estimate-' + str(i)
            row, obs = copy.deepcopy(h), copy.deepcopy(o)
            row['call_id'] = call_id
            row['result']['meta']['call_id'] = call_id
            row['result']['data']['estimate_id'] = estimate_id
            obs.update(event_id=event_id, result=row['result'])
            state['history'].append(row)
            state['observations'].append(obs)
            state['estimates'][estimate_id] = row['result']['data']
        before = copy.deepcopy(state)
        view = self.compact_state(state)
        self.assertEqual(view['state']['first_over_budget_observation_by_option']['graduation_video'], first)
        latest = view['state']['observed_constraints_by_option']['graduation_video']
        self.assertEqual((latest['quoted_cost'], latest['budget_gap'], latest['source_call_id']), (12, 2, 'repeat-call-1999'))
        self.assertFalse(latest['save_ready'])
        self.assertLess(len(json.dumps(view)), len(json.dumps(initial)) + 600)
        self.assertEqual(view['action_history_summary']['groups'][-1]['count'], 2001)
        self.assertLessEqual(len(view['observation_refs']), 2)
        self.assertNotIn('tool_history', view)
        self.assertEqual(state, before)

    def test_context_preserves_current_matching_ids_and_clock(self):
        self.run_agent(rounds=4)
        state = self.store.get(self.task['task_id'])
        view = self.compact_state(state)
        s = view['state']['available_scripts'][-1]
        e = next(e for e in view['state']['available_estimates'] if e['script_id'] == s['script_id'])
        self.assertEqual(s['plan_version'], state['plan_version'])
        self.assertEqual(e['plan_version'], s['plan_version'])
        self.assertTrue(e['save_ready'])
        self.assertIn(e['estimate_id'], view['state']['available_ids']['estimate_ids'])
        self.assertIn(e['estimate_id'], view['state']['dependencies_by_option'][0]['save_ready_estimate_ids'])
        self.assertEqual(view['current_time'], NOW.isoformat())
        self.assertEqual(view['remaining_rounds'], 4)
        self.assertNotIn('next_action', json.dumps(view))

    def test_context_preserves_failure_on_disk_and_unresolved_error(self):
        search = call('search_materials', {'query': '校园毕业季', 'required_kinds': ['campus_photo','graduate_message']}, '测试')
        generate = call('generate_script', {'option_id': 'graduation_video','material_ids': ['campus-photo-01','graduate-message-01']}, '测试')
        self.run_agent(model_factory=lambda: ScriptedModel([search,generate,search]), rounds=3, fail='generate_script')
        state = self.store.get(self.task['task_id'])
        disk = {p: p.read_bytes() for p in self.base.glob('*.jsonl')}
        view = self.compact_state(state)
        self.assertEqual(view['state']['unresolved_tool_errors'][0]['result']['code'], 'injected_failure')
        self.assertEqual(view['state']['recent_errors'][0]['source_call_id'], state['history'][1]['call_id'])
        self.assertEqual(self.store.get(self.task['task_id']), state)
        self.assertTrue(all(p.read_bytes() == data for p,data in disk.items()))
        self.assertEqual(state['steps'][1]['status'], 'failed')

    def test_context_successful_retry_does_not_erase_failed_attempt(self):
        search = call('search_materials', {'query':'校园毕业季','required_kinds':['campus_photo','graduate_message']}, '测试')
        generate = call('generate_script', {'option_id':'graduation_video','material_ids':['campus-photo-01','graduate-message-01']}, '测试')
        self.run_agent(model_factory=lambda: ScriptedModel([search,generate,generate]), rounds=3, fail='generate_script')
        state = self.store.get(self.task['task_id'])
        view = self.compact_state(state)
        self.assertEqual(view['state']['unresolved_tool_errors'], [])
        self.assertEqual(view['state']['recent_errors'][0]['result']['status'], 'error')
        self.assertEqual([g['status'] for g in view['action_history_summary']['groups']], ['success','error','success'])
        self.assertEqual(state['steps'][1]['status'], 'failed')
        self.assertEqual(state['steps'][2]['status'], 'succeeded')

    def test_context_oversize_refuses_instead_of_truncating_evidence(self):
        self.run_agent(rounds=1)
        state = self.store.get(self.task['task_id'])
        state['task']['theme'] = '长' * 20000
        original = copy.deepcopy(state)
        with self.assertRaisesRegex(ModelError, '16000'):
            self.compact_state(state)
        self.assertEqual(state, original)

    def test_context_budget_summary_is_only_observed_facts(self):
        from content_agent.context import build_model_context
        self.task["budget"] = 10
        self.run_agent(rounds=2)
        state = self.store.get(self.task["task_id"])
        original = copy.deepcopy(state)
        tools = BusinessTools(ROOT / "data", lambda: NOW)
        compact = build_model_context({"task": state["task"], "state": state, "catalog": tools.catalog, "current_time": NOW.isoformat(), "remaining_rounds": 8})
        constraints = compact["state"]["observed_constraints_by_option"]
        self.assertEqual(list(constraints), ["graduation_video"])
        self.assertEqual(constraints["graduation_video"]["quoted_cost"], 12)
        self.assertEqual(constraints["graduation_video"]["budget_gap"], 2)
        self.assertFalse(constraints["graduation_video"]["feasible"])
        self.assertNotIn("graduation_slides", constraints)
        self.assertEqual(state, original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
