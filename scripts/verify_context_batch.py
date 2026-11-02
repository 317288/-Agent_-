"""One predeclared batch, unchanged stage-4 evaluator, no automatic reruns."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
from scripts.verify_stage4 import evaluate, INSTRUCTIONS, write
from content_agent.context import build_model_context
from content_agent.model import SYSTEM
from content_agent.schemas import ACTION_SCHEMA

CASES = [('A-01','A'),('A-02','A'),('B-01','B'),('B-02','B'),('N-01','N'),('C-01','C')]
CONFIG = {'AGENT_BASE_URL':'http://localhost:11434/v1', 'AGENT_MODEL':'qwen2.5:7b-instruct-q4_K_M',
          'AGENT_API_KEY':'', 'AGENT_PROTOCOL':'ollama', 'AGENT_RESPONSE_FORMAT':'json_schema',
          'AGENT_TIMEOUT_SECONDS':'180','AGENT_NUM_CTX':'8192'}
HEADROOM = 2048


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sources():
    return {str(p.relative_to(ROOT)):sha(p) for directory in ('src','tests','scripts','data')
            for p in (ROOT / directory).rglob('*') if p.suffix in ('.py','.json')}


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def events_at(directory):
    return [json.loads(x) for x in (directory/'trace.jsonl').read_text(encoding='utf-8').splitlines()]


def metrics(directory):
    events = events_at(directory)
    actions = {e['iteration']:e for e in events if e['event_type']=='model_action'}
    rows = []
    for e in events:
        if e['event_type'] != 'model_request':
            continue
        raw = (directory/e['payload']['request_body_ref']).read_bytes()
        body = json.loads(raw)
        text = body['messages'][1]['content']
        tokens = actions.get(e['iteration'],{}).get('payload',{}).get('model_metadata',{}).get('usage') or {}
        pt, ct = tokens.get('prompt_tokens'), tokens.get('completion_tokens')
        rows.append({'iteration':e['iteration'], 'request_bytes':len(raw), 'user_content_characters':len(text),
                     'user_content_bytes':len(text.encode('utf-8')), 'prompt_tokens':pt, 'completion_tokens':ct,
                     'output_headroom_tokens':8192-pt if pt is not None else None,
                     'minimum_2048_headroom':pt is not None and pt <= 8192-HEADROOM,
                     'hash_matches':hashlib.sha256(raw).hexdigest()==e['payload']['body_sha256']})
    return rows


def repeat_metrics(events):
    """Count repeated unchanged searches/quotes; retries after errors excluded.

    No semantic score from prose. Quote compared excluding timestamps/IDs; same
    arguments and same business result as an earlier successful call is redundant.
    """
    seen, repeats = set(), []
    for e in events:
        if e['event_type'] != 'tool_result':
            continue
        p=e['payload']; r=p['result']; d=r.get('data') or {}
        if r['status'] != 'success' or p['tool'] not in ('search_materials','estimate_plan'):
            continue
        facts = d.get('materials') if p['tool']=='search_materials' else {k:d.get(k) for k in
            ('option_id','script_id','plan_version','estimated_total_cost','duration_minutes','budget_gap','time_gap_seconds','feasible','save_ready','missing','checks')}
        key=json.dumps([p['tool'],p['arguments'],facts],sort_keys=True,ensure_ascii=False)
        if key in seen: repeats.append({'iteration':e['iteration'],'tool':p['tool'],'call_id':p['call_id']})
        seen.add(key)
    return {'definition':'Earlier success with identical arguments and unchanged search/estimate business facts; excludes error retries and script-bound new estimates.',
            'count':len(repeats),'events':repeats}


def audit_views(directory, events):
    facts = {e['payload']['call_id']:e for e in events if e['event_type']=='tool_result'}
    checks=[]
    for e in events:
        if e['event_type']!='model_request': continue
        body=load(directory/e['payload']['request_body_ref'])
        c=json.loads(body['messages'][1]['content']); s=c['state']
        checks.append('tool_history' not in c and 'history' not in s and 'next_action' not in c)
        for name in ('observed_constraints_by_option','first_over_budget_observation_by_option'):
            for option,v in s[name].items():
                source=facts[v['source_call_id']]; d=source['payload']['result']['data']
                checks.append(source['iteration'] < e['iteration'] and source['event_id']==v['source_event_id'] and d['option_id']==option)
                checks.append(all(v[k]==d[k] for k in ('estimate_id','script_id','plan_version','budget_gap','time_gap_seconds','feasible','save_ready','evaluated_at')) and v['quoted_cost']==d['estimated_total_cost'])
        critical=[x for x in facts.values() if x['iteration'] < e['iteration'] and
                  (x['payload']['result']['code']=='injected_failure' or (x['payload']['tool']=='estimate_plan' and (x['payload']['result'].get('data') or {}).get('budget_gap',0)>0))]
        if critical:
            first=critical[0]; p=first['payload']
            if p['result']['code']=='injected_failure':
                checks.append(any(v.get('source_call_id')==p['call_id'] for v in s['recent_errors']+s['unresolved_tool_errors']))
            else:
                checks.append(s['first_over_budget_observation_by_option'][p['result']['data']['option_id']]['source_call_id']==p['call_id'])
    return bool(checks) and all(checks)


def compare(root):
    comparisons=[]
    for name in ('A-02','B-02'):
        directory=ROOT/'examples/live/stage4'/name/'run'
        final=load(directory/'output.json')['result']
        events=events_at(directory)
        for request in [e for e in events if e['event_type']=='model_request' and e['iteration'] in (1,5,8,9,10)]:
            old=load(directory/request['payload']['request_body_ref'])
            c=json.loads(old['messages'][1]['content'])
            previous=[e for e in events if e['event_type']=='tool_result' and e['iteration']<request['iteration']]
            ids={e['payload']['call_id'] for e in previous}
            state={'task':c['task'],'status':'running','plan_version':0,'materials':{},'scripts':{},'estimates':{},
                   'history':[h for h in final['tool_results'] if h['call_id'] in ids],
                   'steps':[s for s in final['steps'] if s['call_id'] in ids],'observations':[]}
            for e in previous:
                p=e['payload']; d=p['result'].get('data') or {}
                state['observations'].append({'event_id':e['event_id'],'tool':p['tool'],'result':p['result']})
                if p['tool']=='search_materials': state['materials'].update({m['material_id']:m for m in d.get('materials',[])})
                if p['tool']=='generate_script' and 'script_id' in d:
                    state['scripts'][d['script_id']]=d; state['plan_version']=d['plan_version']
                if p['tool']=='estimate_plan' and 'estimate_id' in d: state['estimates'][d['estimate_id']]=d
            view=build_model_context({'task':c['task'],'state':state,'catalog':c['catalog'],'current_time':c['current_time'],'remaining_rounds':c['remaining_rounds']})
            new={**old,'messages':[old['messages'][0],{'role':'user','content':json.dumps({'action_schema':c['action_schema'],'tool_notes':c['tool_notes'],**view},ensure_ascii=False,allow_nan=False)}]}
            file=root/'offline-comparison'/f'{name}-{request["iteration"]:02d}.json'
            write(file,new)
            oldmetric=next(m for m in metrics(directory) if m['iteration']==request['iteration'])
            comparisons.append({'case':name,'iteration':request['iteration'],'old_actual':oldmetric,
                                'new_reconstructed_request_bytes':len(json.dumps(new,ensure_ascii=False,allow_nan=False).encode()),
                                'new_reconstructed_user_characters':len(new['messages'][1]['content']),
                                'new_prompt_tokens':None,'note':'Offline same-state projection only; not sent to model, no invented token count.'})
    write(root/'before-after-size.json',comparisons)


def freeze(root):
    if (root/'frozen.json').exists(): raise RuntimeError('Already frozen; do not overwrite')
    compare(root)
    write(root/'frozen.json',{'created_at':datetime.now(timezone.utc).isoformat(),'cases':CASES,'max_rounds':10,
          'config':{k:v for k,v in CONFIG.items() if k!='AGENT_API_KEY'},'temperature':0,'minimum_output_headroom_tokens':HEADROOM,
          'system_sha256':hashlib.sha256(SYSTEM.encode()).hexdigest(),
          'schema_sha256':hashlib.sha256(json.dumps(ACTION_SCHEMA,ensure_ascii=False).encode()).hexdigest(),
          'instructions':INSTRUCTIONS,'normal_input':'same stage-3 theme/budget=20/deadline=now+24h, no instructions',
          'source_sha256':sources(),'repeat_definition':repeat_metrics([])['definition']})


def run(root):
    manifest=load(root/'frozen.json')
    assert sources()==manifest['source_sha256'], 'Source changed after freeze'
    # Local inventory only; never pull/download or invoke a different model.
    inventory={}
    for endpoint in ('version','tags'):
        with urllib.request.urlopen('http://localhost:11434/api/'+endpoint,timeout=15) as r: inventory[endpoint]=json.load(r)
    write(root/'server-inventory.json',inventory)
    results=[]
    for name,scenario in CASES:
        assert sources()==manifest['source_sha256'], 'Source changed during batch'
        directory=root/name
        directory.mkdir(exist_ok=False)
        task={'task_id':'ctx-'+scenario.lower()+'-'+uuid4().hex[:12], 'theme':'校园毕业季','budget':{'A':10,'B':20,'N':20,'C':6}[scenario],
              'deadline':(datetime.now(timezone(timedelta(hours=8)))+timedelta(days=1)).replace(microsecond=0).isoformat()}
        if scenario!='N': task['instructions']=INSTRUCTIONS[scenario]
        write(directory/'task.json',task)
        db=ROOT/'runtime'/(task['task_id']+'.sqlite3'); evidence=directory/'run'
        cmd=[sys.executable,'-X','utf8',str(ROOT/'run.py'),'--input',str(directory/'task.json'),'--mode','live','--db',str(db),'--output-dir',str(evidence),'--max-rounds','10']
        if scenario=='B': cmd+=['--fail-once','generate_script']
        write(directory/'invocation.json',{'command':cmd,'task_id':task['task_id'],'config':manifest['config'],'frozen_manifest_sha256':sha(root/'frozen.json')})
        print('Starting '+name+' '+task['task_id'],flush=True)
        with (directory/'stdout.json').open('w',encoding='utf-8') as out,(directory/'stderr.txt').open('w',encoding='utf-8') as err:
            proc=subprocess.run(cmd,cwd=ROOT,env={**os.environ,**CONFIG},stdout=out,stderr=err)
        output=load(evidence/'output.json'); events=events_at(evidence)
        with sqlite3.connect(str(db)) as conn:
            row=conn.execute('SELECT document FROM plans WHERE task_id=?',(task['task_id'],)).fetchone()
            plan=json.loads(row[0]) if row else None
            snapshot=json.loads(conn.execute('SELECT snapshot FROM tasks WHERE task_id=?',(task['task_id'],)).fetchone()[0])
        report=evaluate(scenario if scenario!='N' else 'B',output,events,plan,snapshot)
        if scenario=='N':
            report['scenario']='N'
            report['criteria']={'normal_saved':output['status']=='completed'}
            report['scenario_passed']=output['status']=='completed' and all(report['checks'].values())
        report['checks']['export_consistent']=load(evidence/'plan.json')==plan if plan else not (evidence/'plan.json').exists()
        report['checks']['compact_facts_match_original_tools']=audit_views(evidence,events)
        sizes=metrics(evidence)
        report.update(case=name,task_id=task['task_id'],exit_code=proc.returncode,request_metrics=sizes,
                      repeats=repeat_metrics(events),output_headroom_verified=bool(sizes) and all(m['minimum_2048_headroom'] for m in sizes))
        report['scenario_passed']=report['scenario_passed'] and all(report['checks'].values())
        write(directory/'verification.json',report)
        results.append(report)
        write(root/'summary.json',{'attempts':len(results),'expected_attempts':6,'source_unchanged':sources()==manifest['source_sha256'],
                                 'results':results,'no_reruns':True})
        print(json.dumps({'case':name,'status':report['status'],'passed':report['scenario_passed'],'calls':report['model_calls'],
                          'repeats':report['repeats']['count'],'max_prompt_tokens':max((m['prompt_tokens'] or 0 for m in sizes),default=0),
                          'output_headroom_verified':report['output_headroom_verified']},ensure_ascii=False),flush=True)
    assert sources()==manifest['source_sha256']


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('phase',choices=['freeze','run'])
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    (freeze if args.phase=='freeze' else run)(args.output.resolve())
