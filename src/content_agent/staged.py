"""Explicit model adoption; bounded program production skeleton."""
from datetime import timedelta
from .model import call
from .schemas import schema_issues, ValidationError, parse_time, object_schema, SUMMARY_SCHEMA
from .context import result_view

SYSTEM = '''你负责内容方案选择和异常处理，只输出JSON公开动作，不输出内部思维。
evaluate_option(option_id)：仅评估候选，执行一次估算，不表示采用或开始制作。
proceed_with_plan(option_id, estimate_id)：明确确认采用已有实际估算且预算、时间可行的方案，授权程序生成脚本、绑定重新估算、校验并保存。
stop：停止未完成任务。retry：仅在工具错误或部分成功后，决定重试该工具。
评估结果将交回你。观察可行结果后，要制作就输出proceed_with_plan及真实estimate_id；有效候选报价复用。
预算和时间是硬约束，偏好是软要求；超预算时自行选择其他候选或停止。错误后自行重试、改选或停止。不得虚构结果、ID或成功。
以下两段仅为合成示例，不是当前任务事实；示例名称、ID、金额不可用于当前任务。实际动作只能引用当前工具结果。
示例一：社区阅读节，预算30。首选reading_stage实际估算38、超预算8。
模型：{"action":"evaluate_option","option_id":"reading_leaflet","decision_summary":"首选超预算，评估允许的替代方案"}
工具：reading_leaflet估算24、时间可行、estimate_id=demo-reading-estimate，尚未制作。
模型：{"action":"proceed_with_plan","option_id":"reading_leaflet","estimate_id":"demo-reading-estimate","decision_summary":"实际估算满足约束，确认采用并进入制作"}
示例二：山地观鸟宣传，预算40。已实际评估全部候选bird_exhibit费用55、bird_audio费用47，时间均可行，最低缺口7。
模型：{"action":"stop","decision_summary":"全部候选均超预算，最低费用47，至少缺少7个模拟单位，停止未完成任务"}'''


def available_branches(context):
    state, catalog = context['state'], context['catalog']
    task = state['task']
    now = parse_time(context['current_time'])
    quotes = {}
    for row in state['history']:
        result = row['result']
        quote = result.get('data') or {}
        if (row['tool'] == 'estimate_plan' and result['status'] == 'success'
                and result['meta']['task_id'] == task['task_id'] and quote['option_id'] in catalog['options']
                and quote['catalog_version'] == catalog['catalog_version'] and quote['script_id'] is None
                and not quote['missing'] and state['estimates'].get(quote['estimate_id']) == quote):
            quotes[quote['option_id']] = quote
    def branch(action, extra):
        props = {'action':{'type':'string','enum':[action]}, **extra, 'decision_summary':SUMMARY_SCHEMA}
        return object_schema(props,list(props))
    enum = lambda values: {'type':'string','enum':values}
    result = []
    pending = [option for option in catalog['options'] if option not in quotes]
    if pending:
        result.append(branch('evaluate_option', {'option_id':enum(pending)}))
    for option, quote in quotes.items():
        if (quote['estimated_total_cost'] <= task['budget'] and
                now + timedelta(minutes=quote['duration_minutes']) <= parse_time(task['deadline'])):
            result.append(branch('proceed_with_plan', {'option_id':enum([option]),
                          'estimate_id':enum([quote['estimate_id']])}))
    last = state['history'][-1]['result']
    if last['status'] == 'partial' or (last['status'] == 'error' and last.get('retryable')):
        result.append(branch('retry', {}))
    result.append(branch('stop', {}))
    return result


class StagedModel:
    def __init__(self, live, trace):
        self.live, self.trace = live, trace
        live.staged, live.stage_system = True, SYSTEM
        self.request_observer = None
        self.last_metadata, self.last_public_output = {}, None
        self.action_source = 'program'
        self.adopted = None

    def decide(self, context):
        s, catalog = context['state'], context['catalog']
        iteration = context.get('iteration', len(s['history'])+1)
        self.last_public_output, self.last_metadata = None, {}
        self.action_source = 'program'
        def emit(kind, payload): self.trace.emit(kind,s,payload,iteration)
        def arranged(tool,args,reason):
            emit('program_arrangement',{'tool':tool,'reason':reason,'authorized_plan':self.adopted})
            emit('parameter_completion',{'tool':tool,'arguments':args,'source':'validated choice and persisted real artifacts'})
            if self.last_public_output is None:
                self.last_public_output = {'source':'program','note':'No model request in this step'}
            return call(tool,args,reason)
        if not s['history']:
            emit('stage_transition',{'to':'materials','source':'program'})
            return arranged('search_materials',{'query':s['task']['theme'],'required_kinds':sorted({k for o in catalog['options'].values() for k in o['required_kinds']})},'程序检索素材')
        last=s['history'][-1]; r=last['result']; d=r.get('data') or {}
        if self.adopted and r['status']=='success' and d.get('option_id')==self.adopted['option_id']:
            if last['tool']=='generate_script' and d['plan_version']==s['plan_version']:
                emit('stage_transition',{'to':'bound_estimate','source':'program','confirmation':self.adopted})
                return arranged('estimate_plan',{'option_id':d['option_id'],'script_id':d['script_id']},'确认后的脚本绑定估算')
            if last['tool']=='estimate_plan' and d.get('script_id') and d['save_ready'] and d['plan_version']==s['plan_version']:
                emit('stage_transition',{'to':'save','source':'program','confirmation':self.adopted})
                return arranged('save_plan',{'script_id':d['script_id'],'estimate_id':d['estimate_id'],'plan_version':d['plan_version']},'确认后的绑定估算通过，执行保存复核')
        exceptional=r['status']!='success' or (last['tool']=='estimate_plan' and not d.get('feasible'))
        if exceptional:
            emit('stage_transition',{'to':'model_decision','source':'tool_observation','call_id':last['call_id'],'reason':'error/partial/constraints'})
        specs=available_branches(context)
        self.live.action_schema={'anyOf':specs}
        self.has_remaining_candidate = any(b['properties']['action']['enum'][0] in ('evaluate_option','proceed_with_plan') for b in specs)
        ids={o['result'].get('meta',{}).get('call_id'):o['event_id'] for o in s['observations'] if 'result' in o}
        quotes={}
        for h in s['history']:
            if h['tool']=='estimate_plan' and h['result'].get('data'):
                quotes[h['result']['data']['option_id']]=result_view(h,ids)
        compact={'task':context['task'],'current_time':context['current_time'],'remaining_steps':context['remaining_rounds'],
                 'plan_version':s['plan_version'],'actions':[{k:v['enum'] for k,v in b['properties'].items() if 'enum' in v} for b in specs],'current_problem':'评估尚无报价的候选，或确认采用有效报价；也可以停止',
                 'options':{k:{'title':v['title'],'required_kinds':v['required_kinds']} for k,v in catalog['options'].items()},
                 'last_tool_result':result_view(last,ids),'observed_quotes':quotes,
                 'unfinished_production_steps':['generate_script','estimate_plan bound to script','validate and save_plan'],
                 'material_ids':list(s['materials']),'adopted_plan':self.adopted,
                 'recent_validation_errors':[o for o in s['observations'] if o.get('code')=='invalid_action'][-1:]}
        emit('stage_presented',compact)
        self.live.request_observer=self.request_observer
        try:
            chosen=self.live.decide(compact)
        finally:
            self.last_public_output=self.live.last_public_output
            self.last_metadata=self.live.last_metadata
        emit('model_choice',{'choice':chosen,'public_output':self.last_public_output,'model_metadata':self.last_metadata})
        action=chosen.get('action') if isinstance(chosen,dict) else None
        matches=[schema_issues(chosen,b) for b in specs]
        if not any(not issues for issues in matches):
            raise ValidationError('不属于当前可用动作；未调用工具', min(matches,key=len))
        self.action_source='model'
        reason=chosen['decision_summary']
        if action=='stop': return {'type':'finish','decision_summary':reason}
        if action=='evaluate_option':
            self.adopted=None
            emit('stage_transition',{'to':'candidate_evaluation','source':'model','option_id':chosen['option_id']})
            return arranged('estimate_plan',{'option_id':chosen['option_id']},reason)
        if action=='retry':
            return arranged(last['tool'],last['arguments'],reason)
        estimate=s['estimates'].get(chosen['estimate_id'])
        provenance=next((h for h in s['history'] if h['tool']=='estimate_plan' and h['result']['status']=='success'
                         and (h['result'].get('data') or {}).get('estimate_id')==chosen['estimate_id']
                         and h['result'].get('meta',{}).get('task_id')==s['task']['task_id']),None)
        valid=(estimate and provenance and provenance['result']['data']==estimate
               and estimate['option_id']==chosen['option_id'] and estimate['catalog_version']==catalog['catalog_version']
               and estimate['feasible'] and not estimate['missing'] and estimate['estimated_total_cost']<=s['task']['budget']
               and parse_time(context['current_time'])+timedelta(minutes=estimate['duration_minutes'])<=parse_time(s['task']['deadline']))
        if not valid:
            raise ValidationError('确认被拒绝：估算必须属于本任务、方案匹配、费用及当前时间可行',
                                  [{'path':'$.estimate_id','code':'invalid_confirmation','message':'未进入制作阶段'}])
        self.adopted={'option_id':chosen['option_id'],'estimate_id':chosen['estimate_id'],'confirmation_iteration':iteration}
        emit('stage_transition',{'to':'production','source':'model_confirmation','confirmation':self.adopted})
        return arranged('generate_script',{'option_id':chosen['option_id'],'material_ids':list(s['materials'])},reason)
