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
    def advice_ok(r,s):
        rec = r.get('recommendations') or {}
        return (r['intake_status']=='needs_guidance' and 1 <= len(rec.get('options',[])) <= 3 and
            all(o['suggestion_origin']=='model' and o['evidence_status']=='unverified' for o in rec['options']) and
            s['active_request_version'] is None and rec['generation']['operation_id'])
    for case, text, obj in [('furan','我想知道呋喃基分子有什么特性','呋喃基'),
                            ('battery','我想要找到一种性价比高的电池液','电池液'),
                            ('ceramic','我想研究氮化硼陶瓷，但是不了解应用场景','氮化硼陶瓷')]:
        task_id = create_task('generic-'+args.mode); context = {}
        step(case+'-intent',text,lambda r,s:not r['ready_for_design'] and s['active_request_version'] is None,
            {'updates':[{'field':'research_object','status':'specified','value':obj,'quote':obj}]})
        advice = step(case+'-recommend','你帮我推荐吧',advice_ok,{'actions':['recommend']})
        if advice['judgment']=='error': continue
        step(case+'-select','采用第一个方向，但先不考虑成本',
             lambda r,s: r['request']['fields']['research_object']['source']=='confirmed_suggestion' and not r['ready_for_design'],
             {'actions':['select','edit'],'selected_option':1,'updates':[{'field':'constraints','status':'none','quote':'先不考虑成本','action':'remove','target_value':'成本'}]})
        # Confirmation may legitimately remain blocked by unspecified fields; it must never authorize calculation.
        step(case+'-confirm','确认',lambda r,s:r['calculation_status']=='pending_research_design' and
             (not r['ready_for_design'] or s['active_request_version'] is not None))
        if case == 'ceramic':
            step('ceramic-explicit-request',
                 '我明确只比较氮化硼陶瓷基板，用于电子器件散热；研究目的为比较材料，范围仅材料级不验证器件，功能为传导热量，目标只比较热传导，暂不设置成本限制，服役温度交给研究设计确定。',
                 lambda r,s:r['intake_status']=='needs_confirmation',
                 {'domain':'materials_application','domain_quote':'电子器件散热','updates':[
                    {'field':f,'status':'specified','value':v,'quote':q,**kw} for f,v,q,kw in [
                      ('research_object','氮化硼陶瓷基板','氮化硼陶瓷基板',{}),
                      ('application','电子器件散热','电子器件散热',{}),
                      ('purpose','比较材料','比较材料',{}),
                      ('research_scope','仅材料级不验证器件','仅材料级不验证器件',{}),
                      ('material_function','传导热量','传导热量',{}),
                      ('target_performance','热传导','热传导',{'direction':'比较','action':'replace'})]]+
                    [{'field':'constraints','status':'none','quote':'暂不设置成本限制','action':'replace'},
                     {'field':'work_conditions','status':'open','quote':'服役温度交给研究设计确定'}]})
            step('ceramic-final-confirm','确认',lambda r,s:r['ready_for_design'] and s['active_request_version'] is not None)
    task_id = create_task('generic-boundaries'); context = {}
    step('drug-boundary','我要筛选和靶蛋白结合的小分子药物候选',lambda r,s:r['intake_status']=='unsupported' and not r['ready_for_design'],
         {'domain':'drug_discovery','domain_quote':'药物候选','updates':[{'field':'purpose','status':'specified','value':'筛选药物候选','quote':'药物候选'}]})
    step('unsupported-no-recommendation','请推荐几个方向',lambda r,s:r['intake_status']=='unsupported' and not r.get('recommendations'),{'actions':['recommend']})
    print(json.dumps({'run_id':run_id,**counts}),flush=True)
    return 0 if not counts['failed'] and not counts['errors'] else 1

if __name__ == '__main__': raise SystemExit(main())
