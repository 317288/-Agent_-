import unittest
import test_core
from test_core import ROOT, NOW
from content_agent.staged import StagedModel, available_branches
from content_agent.agent import Trace
from content_agent.tools import BusinessTools

class Stub:
    last_metadata = {}
    last_public_output = None
    def __init__(self, mode='normal'): self.mode=mode; self.calls=0; self.contexts=[]
    def decide(self,c):
        self.calls+=1; self.contexts.append(c)
        last=c['last_tool_result']; d=last['result']['data']
        if last['result']['status']!='success':
            a={'action':'retry'} if self.mode=='retry' else {'action':'stop'}
        elif last['tool']=='search_materials': a={'action':'evaluate_option','option_id':'graduation_video'}
        elif self.mode=='early_stop': a={'action':'stop'}
        elif self.mode=='repeat': a={'action':'evaluate_option','option_id':d['option_id']}
        elif self.mode=='invalid': a={'action':'proceed_with_plan','option_id':d['option_id'],'estimate_id':'invented'}
        elif not d['feasible']: a={'action':'evaluate_option','option_id':'graduation_slides'}
        else: a={'action':'proceed_with_plan','option_id':d['option_id'],'estimate_id':d['estimate_id']}
        a['decision_summary']='deterministic test double'
        self.last_public_output={'parsed_action':a,'parse_ok':True}
        return a

class StageTests(unittest.TestCase):
    def setUp(self):
        self.f=test_core.CoreTests(); self.f.setUp(); self.f.task['budget']=10
    def tearDown(self): self.f.tearDown()
    def run_stage(self,mode='normal',rounds=10,fail=None,partial=None):
        stub=Stub(mode)
        trace=Trace(self.f.base/'stage.jsonl','mock','stub','stage')
        try:
            out=self.f.run_agent(model_factory=lambda:StagedModel(stub,trace),rounds=rounds,fail=fail,partial=partial)
        finally: trace.close()
        return out,stub
    def test_explicit_adoption_then_program_pipeline(self):
        out,stub=self.run_stage()
        self.assertEqual(out['status'],'completed')
        self.assertEqual(out['result']['estimated_total_cost'],8)
        self.assertEqual(stub.calls,3)
        self.assertEqual(len(out['result']['tool_results']),6)
        self.assertEqual(stub.contexts[-1]['last_tool_result']['result']['data']['estimated_total_cost'],8)
        self.assertTrue(out['result']['plan']['estimate']['save_ready'])
    def test_repeated_evaluation_never_adopts_and_consumes_limit(self):
        out,stub=self.run_stage('repeat',rounds=4)
        self.assertEqual(out['result']['incomplete_reason']['code'],'step_limit')
        self.assertEqual(out['result']['artifacts']['scripts'],{})
        self.assertEqual(len(out['result']['tool_results']),2)
        self.assertEqual(stub.contexts[-1]['actions'][0]['option_id'], ['graduation_slides'])
        self.assertEqual(stub.calls,3)
    def test_invalid_confirmation_never_executes_production(self):
        out,stub=self.run_stage('invalid',rounds=4)
        self.assertEqual(out['result']['artifacts']['scripts'],{})
        self.assertEqual(len(out['result']['tool_results']),2)
        self.assertEqual(out['result']['incomplete_reason']['code'],'step_limit')
    def test_stop_with_unquoted_option_preserves_model_reason(self):
        self.f.task['budget']=6
        out,stub=self.run_stage('early_stop')
        self.assertEqual(out['result']['incomplete_reason']['code'],'model_stopped')
        self.assertIsNone(out['result']['incomplete_reason']['feasibility_diagnosis'])

    def test_gap_message_uses_lowest_quote(self):
        self.f.task['budget']=6
        out=self.f.run_agent()
        reason=out['result']['incomplete_reason']
        self.assertEqual(reason['minimum_budget_gap'],2)
        self.assertIn(' 2 ', reason['message'])
        self.assertIn('model_summary',reason)

    def test_reusable_quote_rechecks_time_and_catalog_version(self):
        self.f.task['budget']=20
        self.f.run_agent(rounds=2)
        state=self.f.store.get(self.f.task['task_id'])
        catalog=BusinessTools(ROOT/'data',lambda:NOW).catalog
        context={'state':state,'catalog':catalog,'current_time':NOW.isoformat()}
        def allowed():
            return [b['properties'] for b in available_branches(context)]
        adoption=next(b for b in allowed() if b['action']['enum']==['proceed_with_plan'])
        self.assertEqual(adoption['option_id']['enum'],['graduation_video'])
        self.assertEqual(adoption['estimate_id']['enum'],list(state['estimates']))
        context['current_time']=state['task']['deadline']
        self.assertFalse(any(b['action']['enum']==['proceed_with_plan'] for b in allowed()))
        catalog['catalog_version']='other'
        self.assertIn('graduation_video',allowed()[0]['option_id']['enum'])
    def test_error_returns_to_model_no_automatic_retry(self):
        out,stub=self.run_stage(fail='generate_script')
        self.assertEqual(out['status'],'incomplete')
        self.assertEqual(stub.contexts[-1]['last_tool_result']['result']['status'],'error')
        self.assertEqual(sum(h['tool']=='generate_script' for h in out['result']['tool_results']),1)
    def test_partial_returns_to_model(self):
        out,stub=self.run_stage(partial='generate_script')
        self.assertEqual(out['status'],'incomplete')
        self.assertEqual(stub.contexts[-1]['last_tool_result']['result']['status'],'partial')
    def test_retry_requires_actual_model_choice(self):
        out,stub=self.run_stage('retry',fail='generate_script')
        self.assertEqual(out['status'],'completed')
        self.assertEqual(stub.calls,4)
        self.assertEqual([s['status'] for s in out['result']['steps'] if s['tool']=='generate_script'],['failed','succeeded'])
    def test_program_pipeline_cannot_bypass_limit(self):
        out,stub=self.run_stage(rounds=4)
        self.assertEqual(out['result']['incomplete_reason']['code'],'step_limit')
        self.assertIsNone(out['result']['saved_plan_ref'])
        self.assertEqual(len(out['result']['tool_results']),4)
