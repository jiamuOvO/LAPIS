"""Synthetic generic dialogue acceptance through the real durable intake path."""
import argparse
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import Mock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
import instructor
from openai import OpenAI
import lapis_intake
from lapis import render_intake_result
from lapis_core import digest
from lapis_contract import CONTRACT_VERSION, RULE_VERSION
from lapis_graph import run_intake_turn
from lapis_intake import Extraction, SYSTEM_PROMPT, PROPOSAL_PROMPT
from lapis_proposals import Guidance
from lapis_store import create_task, get_task, initialize_schema
from test_lapis_proposals import proposal


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['mock','real'], default='mock')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if os.getenv('LAPIS_TEST_PG') != '1' or os.getenv('LAPIS_DB_NAME') != 'lapis_test':
        parser.error('Requires isolated lapis_test database.')
    initialize_schema()
    events = []; extractions = []
    original_extract = lapis_intake.extract
    def traced_extract(*args, **kwargs):
        value = original_extract(*args, **kwargs)
        extractions.append(value.model_dump())
        return value
    def response_event(response):
        response.read()
        item = {'status_code':response.status_code}
        if response.status_code == 200:
            body = response.json()
            item.update(model=body.get('model'),usage=body.get('usage'),completion_id=body.get('id'))
        events.append(item)
    model = os.getenv('LAPIS_MODEL','deepseek-flash') if args.mode == 'real' else 'test-double'
    if args.mode == 'real':
        key = os.getenv('LAPIS_API_KEY') or os.getenv('DEEPSEEK_API_KEY')
        if not key:
            parser.error('No API credential; no real test performed.')
        client = instructor.from_openai(OpenAI(api_key=key,base_url=os.getenv('LAPIS_BASE_URL','https://api.deepseek.com'),
            timeout=40,max_retries=0,http_client=httpx.Client(event_hooks={'response':[response_event]})),mode=instructor.Mode.JSON)
    else:
        client = Mock(); client.create.return_value = Guidance.model_validate(proposal())
    prompt_hash = digest({'extract':SYSTEM_PROMPT,'proposal':PROPOSAL_PROMPT})
    baseline = subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    code_hashes = {name:hashlib.sha256((ROOT/name).read_bytes().replace(b'\r\n',b'\n')).hexdigest()
        for name in ('lapis.py','lapis_intake.py','lapis_contract.py','lapis_proposals.py','lapis_guidance.py','lapis_graph.py','lapis_core.py','lapis_store.py')}
    run_id = str(uuid4()); counts = {'passed':0,'failed':0,'errors':0}; context = {}; task_id = None
    args.output.parent.mkdir(parents=True,exist_ok=True)
    def step(name,text,predicate,mock=None):
        nonlocal context
        events.clear(); extractions.clear(); operation_id = str(uuid4()); started = time.perf_counter()
        record = {'run_id':run_id,'mode':args.mode,'step':name,'input':text,'input_context':deepcopy(context),
            'task_id':task_id,'operation_id':operation_id,'baseline_commit':baseline,'code_sha256':code_hashes,
            'model':model,'model_pinning':'provider_alias_unpinned','contract_version':CONTRACT_VERSION,
            'rule_version':RULE_VERSION,'prompt_sha256':prompt_hash,'data_origin':'synthetic_acceptance'}
        try:
            if args.mode == 'mock':
                with patch('lapis_intake.extract',return_value=Extraction.model_validate(mock or {})):
                    out = run_intake_turn(task_id,text,'generic-acceptance',client,model,prompt_hash,operation_id,input_context=context)
            else:
                with patch('lapis_intake.extract', side_effect=traced_extract):
                    out = run_intake_turn(task_id,text,'generic-acceptance',client,model,prompt_hash,operation_id,input_context=context)
            result = out['result']; saved = get_task(task_id)
            context = result['input_context']
            shown = io.StringIO()
            with redirect_stdout(shown): render_intake_result(result)
            record.update(output=out,active_request_version=saved['active_request_version'],displayed_output=shown.getvalue())
            views_path = args.output.with_suffix('.views.md')
            with views_path.open('a',encoding='utf-8') as views:
                views.write('\n## '+name+'\n\n你> '+text+'\n\n```text\n'+shown.getvalue()+'```\n')
            record['judgment'] = 'passed' if predicate(result,saved) else 'failed'
            counts[record['judgment']] += 1
        except Exception as error:
            record.update(judgment='error',error_type=type(error).__name__,error=str(error)[:500])
            counts['errors'] += 1
        record.update(elapsed_seconds=round(time.perf_counter()-started,3),http_calls=deepcopy(events),
            llm_called=bool(events),http_call_count=len(events),extractions=deepcopy(extractions))
        with args.output.open('a',encoding='utf-8') as stream: stream.write(json.dumps(record,ensure_ascii=False,default=str)+'\n')
        print(name,record['judgment'],record['elapsed_seconds'],flush=True)
        return record
    for case, text, obj, goal in [
        ('furan','我想知道呋喃基分子有什么特性','呋喃基','再加工后性能保持'),
        ('battery','我想找性价比高的电池液','电池液','离子传输'),
        ('ceramic','我想研究氮化硼陶瓷，但不了解用途','氮化硼陶瓷','热传导')]:
        task_id=create_task('ui-'+args.mode); context={}
        if args.mode=='mock':
            body=proposal()
            body['options'][0]['label']='合成测试：'+obj+'材料比较'
            body['options'][0]['reason']='软件行为测试替身，不作科研判断'
            body['options'][0]['assumptions']=['该方向仅供软件验收']
            for key,value in {'research_object':obj,'purpose':'比较'+obj,
                'application':{'furan':'可再加工聚合物','battery':'锂离子电池','ceramic':'电子器件散热'}[case],
                'research_scope':'仅比较材料，不验证器件','material_function':'拟承担所选材料功能'}.items():
                body['options'][0]['fields'][key]['value']=value
            client.create.return_value=Guidance.model_validate(body)
        step(case+'-intent',text,lambda r,s:not r['ready_for_design'],
             {'updates':[{'field':'research_object','status':'specified','value':obj,'quote':obj}]+([{'field':'constraints','status':'unclear','value':'性价比高','strength':'preference','quote':'性价比高'}] if case=='battery' else [])})
        recommended=step(case+'-recommend','我不太了解，你有什么推荐吗',
             lambda r,s:bool((r.get('recommendations') or {}).get('options')) and not r['ready_for_design'], {'actions':['recommend']})
        if recommended['judgment']=='error': continue
        choice='采用第一个，但先不考虑成本' if case=='battery' else '采用第一个方向'
        selected=step(case+'-select',choice,lambda r,s:not r['ready_for_design'],
             {'actions':['select','edit'],'selected_option':1,'updates':([
              {'field':'constraints','status':'none','quote':'先不考虑成本','action':'remove','target_value':'性价比高'}] if case=='battery' else [])})
        # One local modification, not a hand-written complete eight-field form.
        edit='本轮目标只比较'+goal+'，其他目标先撤回；用途、范围和材料功能沿用刚才采用的方向，工作条件交给研究设计确定。'
        step(case+'-edit',edit,lambda r,s:not r['ready_for_design'],
             {'actions':['edit','reaffirm'],'reaffirm_fields':['application','research_scope','material_function'],
              'updates':[{'field':'target_performance','status':'specified','value':goal,'direction':'比较','quote':goal,'action':'replace'},
                         {'field':'work_conditions','status':'open','quote':'工作条件交给研究设计确定'}]})
        for n in range(5):
            current=get_task(task_id)['intake_result']
            if current['intake_status']=='needs_confirmation': break
            issue=(current.get('blocking_issues') or [{}])[0]
            field=issue.get('field')
            value=current['request']['fields'].get(field)
            if isinstance(value,list): value=value[0] if value else {}
            rec = get_task(task_id)['intake_state'].get('recommendation_set') or {}
            choice_fields=(rec.get('options') or [{}])[0].get('fields',{})
            proposed=choice_fields.get(field) or {}
            if isinstance(proposed,list): proposed=proposed[0] if proposed else {}
            intended=(value or {}).get('value') or (proposed.get('value') if isinstance(proposed,dict) else proposed)
            answers={
                'purpose': '这轮就比较候选材料，了解它们的差异。',
                'application': '材料用途就采用刚才方向中的'+str(intended or '材料应用场景')+'。',
                'research_object': '研究对象就是'+str(intended or obj)+'。',
                'research_scope':'范围仅限刚才的材料比较，不验证器件；其他用途、功能和目标沿用当前草稿。',
                'material_function':'功能采用刚才提出的'+str(intended or '材料功能')+'，这只是研究意图。',
                'target_performance':'目标只比较'+goal+'，其余目标撤回。',
                'work_conditions':'条件暂不预设，研究设计阶段确定。',
                'constraints':'本轮不设置成本约束，先前的性价比要求撤回，其他约束沿用。'}
            answer=answers.get(field,'沿用当前草稿中已经明确的材料用途和目标，尚未指定的条件交研究设计确定。')
            updates=[]
            if field in {'purpose','application','research_object','research_scope','material_function'}:
                updates=[{'field':field,'status':'specified','value':intended or answer,'quote':answer}]
            elif field=='target_performance': updates=[{'field':field,'status':'specified','value':goal,'direction':'比较','quote':goal,'action':'replace'}]
            elif field=='constraints': updates=[{'field':field,'status':'none','quote':answer,'action':'replace'}]
            else: updates=[{'field':'work_conditions','status':'open','quote':answer}]
            step(case+'-clarify-'+str(n+1),answer,lambda r,s:not r['ready_for_design'],
                 {'actions':['inform','reaffirm'],'updates':updates,'resolve_issue_ids':[i['id'] for i in current.get('blocking_issues',[]) if i.get('field')==field and i.get('id')],
                  'reaffirm_fields':['work_conditions','target_performance','constraints','material_function']})
        # Hard end-to-end criterion: every case must reach actual confirmation.
        final=step(case+'-confirm','确认',lambda r,s:r['ready_for_design'] and s['active_request_version'] is not None)
        if final['judgment']=='passed':
            task_env=os.environ.copy()
            task_env['LAPIS_API_KEY']=task_env.get('LAPIS_API_KEY') or task_env.get('DEEPSEEK_API_KEY') or 'test-no-api-call'
            recovered=subprocess.run([sys.executable,'-X','utf8',str(ROOT/'lapis.py'),'chat','--task-id',task_id],
                input='/show\n/sources\n/exit\n',text=True,encoding='utf-8',capture_output=True,env=task_env,timeout=30)
            good=recovered.returncode==0 and all(label+'：' in recovered.stdout for label in lapis_intake.LABELS.values())
            counts['passed' if good else 'failed']+=1
            record={'run_id':run_id,'step':case+'-fresh-process-resume','mode':args.mode,'task_id':task_id,
                    'judgment':'passed' if good else 'failed','displayed_output':recovered.stdout,'stderr':recovered.stderr,
                    'model_called':False,'baseline_commit':baseline,'code_sha256':code_hashes}
            with args.output.open('a',encoding='utf-8') as stream:stream.write(json.dumps(record,ensure_ascii=False)+'\n')
            with args.output.with_suffix('.views.md').open('a',encoding='utf-8') as views:
                views.write('\n## '+case+'-fresh-process-resume\n\n```text\n'+recovered.stdout+'```\n')
            print(case+'-fresh-process-resume',record['judgment'],flush=True)
    print(json.dumps({'run_id':run_id,**counts}),flush=True)
    return 0 if not counts['failed'] and not counts['errors'] else 1

if __name__=='__main__': raise SystemExit(main())
