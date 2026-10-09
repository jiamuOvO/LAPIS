"""Fixed and independently simulated users; isolated PG and bounded actual HTTP calls."""
import argparse,io,json,os,sys,time,subprocess,hashlib
from pathlib import Path
from uuid import uuid4
from copy import deepcopy
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import httpx,instructor
from openai import OpenAI
from pydantic import BaseModel,ConfigDict,Field
from lapis_intake import SYSTEM_PROMPT,PROPOSAL_PROMPT,LABELS
from lapis_graph import run_intake_turn
from lapis_store import create_task,get_task,initialize_schema
from lapis_core import digest
from lapis import render_intake_result
from test_lapis_proposals import proposal
from lapis_proposals import Guidance,generate_recommendations

class UserReply(BaseModel):
 model_config=ConfigDict(extra='forbid')
 text:str=Field(min_length=1,max_length=1200)
 decisions:list[str]=Field(default_factory=list)
 stop:str='continue'

USER_PROMPT="""你是材料研究软件的独立模拟用户，不是测试修复者。你只能看到自己的背景和产品实际显示对话。按背景自然交流，一轮最多两三句话，不能一次填八项，不能为了产品过关改变既定意图。初学者可以从所见推荐产生新决定，写入decisions；不确定保持不确定。引用提案不代表要求。纠正误解和暂停是合理行为。未知不填造科学参数，不能把材料功能当已证实。不要输出产品内部字段或测试答案。确认前应看完整规约；需要等待则stop=pause，确认完stop=confirm。输出text/decisions/stop。"""

def main():
 p=argparse.ArgumentParser();p.add_argument('--mode',choices=['fixed','simulated'],required=True);p.add_argument('--scenarios',default='long_additive');p.add_argument('--set',choices=['development','holdout'],default='development');p.add_argument('--max-calls',type=int,default=60);p.add_argument('--max-tokens',type=int,default=400000);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 if os.getenv('LAPIS_TEST_PG')!='1' or os.getenv('LAPIS_DB_NAME')!='lapis_test':p.error('Requires isolated lapis_test')
 initialize_schema();run=str(uuid4());calls=[];current_role='product';tokens=0
 def before(request):
  if len(calls)>=a.max_calls or tokens>=a.max_tokens:raise RuntimeError('Acceptance budget exhausted')
  calls.append({'role':current_role,'started':time.perf_counter()})
 def after(response):
  nonlocal tokens
  response.read();row=calls[-1];row['seconds']=time.perf_counter()-row.pop('started');row['status']=response.status_code
  if response.status_code==200:
   body=response.json();row['usage']=body.get('usage');row['server_model']=body.get('model');tokens+=(body.get('usage') or {}).get('total_tokens',0)
 key=os.getenv('LAPIS_API_KEY') or os.getenv('DEEPSEEK_API_KEY')
 if not key:p.error('API credential unavailable')
 client=instructor.from_openai(OpenAI(api_key=key,base_url=os.getenv('LAPIS_BASE_URL','https://api.deepseek.com'),timeout=45,max_retries=0,http_client=httpx.Client(event_hooks={'request':[before],'response':[after]})),mode=instructor.Mode.JSON)
 model=os.getenv('LAPIS_MODEL','deepseek-flash');prompt=digest({'extract':SYSTEM_PROMPT,'proposal':PROPOSAL_PROMPT});baseline=subprocess.check_output(['git','rev-parse','HEAD'],text=True,cwd=ROOT).strip()
 scenarios=json.loads((ROOT/'tests/fixtures/intake_dialogues'/ (a.set+'.json')).read_text(encoding='utf-8'));chosen=[s for s in scenarios if a.scenarios=='all' or s['id'] in a.scenarios.split(',')]
 if not chosen:p.error('No selected scenarios')
 if a.mode=='fixed' and any(s['id'] not in {'long_additive','multisentence','indicator'} for s in chosen):p.error('Fixed replay supports long_additive, multisentence, indicator; other backgrounds require simulated mode')
 a.output.mkdir(parents=True,exist_ok=True)
 manifest={'run_id':run,'mode':a.mode,'set':a.set,'commit':baseline,'model_alias':model,'seed':'provider_unavailable','prompt_hash':prompt,'user_prompt_hash':digest(USER_PROMPT),'dirty':bool(subprocess.check_output(['git','status','--porcelain'],text=True,cwd=ROOT).strip()),'origin':'synthetic','max_calls':a.max_calls,'max_tokens':a.max_tokens,'code_hashes':{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in ROOT.glob('lapis*.py')}}
 (a.output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
 verdicts=[]
 for scene in chosen:
  task=create_task('dialogue-acceptance');context={};transcript=[];previous={};metric_id=None;failed=[];decisions=[];last=None;paused=False
  fixed=['我想研究呋喃分子的抗氧化性','高分子材料的抗氧化添加剂','比较 吧','你推荐一下','__select__','__purpose__','__assumptions__','__limitations__','氧化诱导期','随便你，无所谓这些','没有约束条件','都研究','都包含','没有不研究的','之前不是选过了吗','你推荐吧','可以了，进行下一步吧','确认'] if scene['id']=='long_additive' else (['研究储能电池电解液，关注传输和稳定性。不能使用含氟添加剂，尽量低成本。','工作条件还不知道，范围就是这轮电解液材料研究。','可以下一步了吗','确认'] if scene['id']=='multisentence' else ['研究高分子抗氧化添加剂，关注抗氧化性。','也记录氧化诱导期，条件和方法还不知道。','范围仅限这轮添加剂研究，没有其他用途。','可以下一步了吗','确认'])
  # Only this original-case recommendation is a controlled fixture. Product extraction remains real.
  body=proposal();option=body['options'][0]
  option.update(label='不同呋喃分子在高分子基体中的抗氧化添加剂效果比较',reason='用户已明确要比较呋喃分子作为高分子抗氧化添加剂的效果，该方向直接对应这一意图，并把比较对象限定为有边界的呋喃分子集合，便于后续确定评价指标和基体。',assumptions=['存在一组可比较的呋喃分子候选，且它们可作为高分子材料的添加剂进行考察','抗氧化性可通过某种尚未指定的实验或分析指标进行相对比较'],limitations=['未指定高分子基体、加工条件、服役温度和氧化评价方法，无法判断比较结果的外推范围','呋喃分子是否普遍具有抗氧化添加剂功能尚未核验，不能断言其已具备该性能'],clarifications=['具体候选与评价方法留研究设计'],source_refs=[])
  for f,v in {'purpose':'比较不同呋喃分子作为高分子材料抗氧化添加剂的效果','research_object':'呋喃分子','application':'高分子材料的抗氧化添加剂','research_scope':'比较不同呋喃分子作为高分子材料抗氧化添加剂的效果','material_function':'拟承担抗氧化添加剂作用'}.items():option['fields'][f]={'status':'specified','value':v}
  option['fields']['target_performance']=[{'status':'specified','value':'抗氧化性','direction':'比较'}]
  second=deepcopy(option);second['label']='加工/热氧化功能考察';second['fields']['purpose']['value']='考察加工与热氧化功能'
  third=deepcopy(option);third['label']='相容性关联研究';third['fields']['purpose']['value']='考察相容性关联'
  body['options']=[option,second,third]
  def recommended(*args,**kwargs):
   if scene['id']=='long_additive' and a.mode=='fixed':
    proxy=type('FixtureClient',(),{'create':lambda self,**kw:Guidance.model_validate(body)})()
    result=generate_recommendations(proxy,*args[1:],**kwargs)
    result["generation"]["model"]="controlled-recommendation-fixture"
    return result
   return generate_recommendations(*args,**kwargs)
  try:
   for n in range(18 if a.mode=='fixed' and scene['id']=='long_additive' else 12):
    if a.mode=='fixed':
     if n>=len(fixed):break
     text=fixed[n]
     rec=(get_task(task)['intake_state'].get('recommendation_set') or {}).get('options',[])
     first=rec[0] if rec else option
     text={'__select__':'就用这个 '+first['label']+'：'+first['reason'],'__purpose__':'拟研究：'+first['fields']['purpose']['value'],'__assumptions__':'假设：'+'；'.join(first['assumptions']),'__limitations__':'限制：'+'；'.join(first['limitations'])}.get(text,text)
     user_decisions=[]
    else:
     current_role='simulated_user'
     opts={'extra_body':{'thinking':{'type':'disabled'}}} if os.getenv('LAPIS_BASE_URL','https://api.deepseek.com').startswith('https://api.deepseek.com') else {}
     user=client.create(model=model,response_model=UserReply,messages=[{'role':'system','content':USER_PROMPT},{'role':'user','content':json.dumps({'background':scene['background'],'visible_dialogue':transcript},ensure_ascii=False)}],max_retries=1,max_tokens=550,temperature=0,**opts)
     text=user.text;user_decisions=user.decisions;decisions.extend(user_decisions);paused=user.stop=='pause'
    current_role='product';start=time.perf_counter();old=deepcopy(previous)
    with patch('lapis_intake.generate_recommendations',side_effect=recommended):out=run_intake_turn(task,text,'dialogue-acceptance',client,model,prompt,str(uuid4()),input_context=context)
    last=out['result'];context=last['input_context'];previous=last['request']['fields'];view=last['view']['text'];transcript.extend([{'role':'user','text':text},{'role':'assistant','text':view}])
    goals=previous['target_performance'];fresh_errors=[]
    if '氧化诱导期' in text and not text.startswith(('限制','假设')):
     found=[g for g in goals if '氧化诱导期' in (g.get('value') or '')]
     if not found:fresh_errors.append('明确指标未进入当前规约')
     elif not metric_id:metric_id=found[0]['id']
    if metric_id and not any(g.get('id')==metric_id and '氧化诱导期' in (g.get('value') or '') for g in goals):fresh_errors.append('无意丢失已选指标')
    if last['ready_for_design'] and text not in {'确认','确认继续','同意','按此继续','就按这个'}:fresh_errors.append('没有明确确认却进入研究设计')
    if text=='没有约束条件' and not any(c.get('status')=='specified' and c.get('strength')=='hard' for c in (old.get('constraints') or [])) and not any(c.get('status')=='none' for c in previous['constraints']):fresh_errors.append('明确无约束没有记录为none')
    if text=='没有不研究的' and previous['research_scope'].get('value')==text:fresh_errors.append('正式范围脱离已有上下文')
    if text.startswith(('假设：','限制：','拟研究：')) and old and previous!=old:fresh_errors.append('复制提案改变研究字段')
    old_ids={g['id']:g for g in old.get('target_performance',[]) if g.get('status')=='specified'}
    new_ids={g.get('id') for g in goals}
    edits=[d['operation'] for d in last.get('operation_decisions',[]) if d.get('decision')=='accepted' and d['operation']['field']=='target_performance' and d['operation']['action'] in {'remove','replace'}]
    for gid in old_ids.keys()-new_ids:
     if not any(e['action']=='replace' or e.get('target_id')==gid or e.get('target_value')==old_ids[gid].get('value') for e in edits):fresh_errors.append('目标条目消失但没有接受的撤回/替换操作')
    diff={f:{'before':old.get(f),'after':v} for f,v in previous.items() if old.get(f)!=v}
    row={'run_id':run,'scenario':scene['id'],'task_id':task,'turn':n+1,'input':text,'actual_display':view,'interpretation':last.get('interpretation'),'operation_decisions':last.get('operation_decisions'),'field_diff':diff,'request':last['request'],'result_status':last['intake_status'],'context':context,'seconds':time.perf_counter()-start,'errors':fresh_errors,'user_decisions':user_decisions,'mode':('hybrid_fixed_recommendation_real_extraction' if a.mode=='fixed' and scene['id']=='long_additive' else a.mode)}
    with (a.output/'turns.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(row,ensure_ascii=False,default=str)+'\n')
    with (a.output/'actual-display.md').open('a',encoding='utf-8') as f:f.write('\n## '+scene['id']+' '+str(n+1)+'\n你> '+text+'\n```text\n'+view+'```\n')
    print(scene['id'],n+1,last['intake_status'],fresh_errors,flush=True);failed.extend(fresh_errors)
    if fresh_errors:break
    if a.mode=='simulated' and (last['ready_for_design'] or paused):break
   good=last and not failed and (last['ready_for_design'] or scene['terminal']=='pause' and paused or scene['terminal']=='blocked' and not last['ready_for_design'] and bool(last['blocking_issues']))
   if scene['id']=='long_additive' and a.mode=='fixed' and not metric_id:good=False;failed.append('未实际验证指标采用及保留')
   if good and last['ready_for_design']:
    child=subprocess.run([sys.executable,'-X','utf8',str(ROOT/'lapis.py'),'chat','--task-id',task],input='/show\n/exit\n',text=True,encoding='utf-8',capture_output=True,timeout=30)
    good=child.returncode==0 and all(x+'：' in child.stdout for x in LABELS.values()) and (not metric_id or '氧化诱导期' in child.stdout)
    (a.output/(scene['id']+'-resume.txt')).write_text(child.stdout+child.stderr,encoding='utf-8')
   verdicts.append({'scenario':scene['id'],'task_id':task,'verdict':'passed' if good else 'failed','errors':failed,'active_request_version':get_task(task)['active_request_version']})
  except Exception as e:verdicts.append({'scenario':scene['id'],'verdict':'error','error_type':type(e).__name__,'error':str(e)[:400]})
  with (a.output/'private-user-decisions.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps({'scenario':scene['id'],'background':scene['background'],'decisions':decisions},ensure_ascii=False)+'\n')
  summary={'run_id':run,'verdicts':verdicts,'calls':calls,'total_tokens':tokens,'semantic_review':'requires_human_review','not_scientific_evidence':True}
  (a.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
  if len(calls)>=a.max_calls or tokens>=a.max_tokens:break
 print(json.dumps(summary,ensure_ascii=False,default=str),flush=True)
 return 0 if verdicts and all(v['verdict']=='passed' for v in verdicts) else 1

if __name__=='__main__':raise SystemExit(main())
