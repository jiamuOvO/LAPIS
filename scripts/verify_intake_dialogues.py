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
from lapis_graph import run_intake_turn, build_intake_graph
from lapis_store import create_task,get_task,initialize_schema,db_config
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.conninfo import make_conninfo
from lapis_core import digest
from lapis import render_intake_result
from test_lapis_proposals import proposal
from lapis_proposals import Guidance,generate_recommendations

class UserReply(BaseModel):
 model_config=ConfigDict(extra='forbid')
 text:str=Field(min_length=1,max_length=1200)
 decisions:list[str]=Field(default_factory=list)
 stop: str = Field(default='continue', pattern='^(continue|pause|confirm)$')

USER_PROMPT="""你是材料研究软件的独立模拟用户，不是测试修复者。你只能看到自己的背景和产品实际显示对话。按背景自然交流，一轮最多两三句话。始终保留背景中明确的基本用途与要求；产品反问不能让你把已知用途改成未知，具体器件细分未知也不等于基本用途未知。不能一次填八项，不能为了产品过关改变既定意图。初学者可以从所见推荐产生新决定，写入decisions；不确定保持不确定。引用提案不代表要求。纠正误解和暂停是合理行为。未知不填造科学参数，不能把材料功能当已证实。不要输出产品内部字段或测试答案。确认前应看完整规约；只有发言明确请求暂停、暂不继续时stop=pause；正在提问、纠正、请求修改并等待答复时必须continue，不能仅因等待标pause。背景有“随后/后来”时按多轮顺序行动，不在首句假装已有未发生的发言。把背景当成自己的想法，不向产品提到私有背景、测试、步骤或模拟规则；不得说产品提到过实际对话里没有的内容。需要提案就自然请求推荐，不能索取不存在的“所见提案”。确认前自行核对背景要求的实际行动是否已在可见对话中发生；未来打算不算已完成的新增、引用或用途改变。未发生就本轮自然执行该行动，不能提前确认结束。认可核对后自然明确确认；stop=confirm必须对应发言中批准整份规约，不只确认某条约束。输出text/decisions/stop。"""

def request_reserve(content):
 """Conservative text byte allowance, excluding HTTP JSON escaping."""
 body=json.loads(content);messages=body.get('messages',[])
 if messages and all(isinstance(m.get('content'),str) for m in messages) and not any(k in body for k in ('tools','response_format','functions')):
  size=sum(len(m['content'].encode('utf-8')) for m in messages)+128*(len(messages)+1)
 else:size=len(content)
 return size+body.get('max_tokens',2600)

def main():
 p=argparse.ArgumentParser();p.add_argument('--mode',choices=['fixed','simulated'],required=True);p.add_argument('--scenarios',default='long_additive');p.add_argument('--set',choices=['development','holdout','supplementary'],default='development');p.add_argument('--max-calls',type=int,default=60);p.add_argument('--max-tokens',type=int,default=400000);p.add_argument('--output',type=Path,required=True);p.add_argument('--resume-from',type=Path);a=p.parse_args()
 if os.getenv('LAPIS_TEST_PG')!='1' or os.getenv('LAPIS_DB_NAME')!='lapis_test':p.error('Requires isolated lapis_test')
 initialize_schema();run=str(uuid4());calls=[];current_role='product';tokens=0;budget_stops=[]
 def before(request):
  reserve=request_reserve(request.content)
  if len(calls)>=a.max_calls or tokens+reserve>a.max_tokens:
   budget_stops.append({'role':current_role,'used_tokens':tokens,'reserved_tokens':reserve,'used_calls':len(calls)})
   raise RuntimeError('Acceptance budget exhausted before request')
  calls.append({'role':current_role,'started':time.perf_counter()})
 def after(response):
  nonlocal tokens
  response.read();row=calls[-1];row['seconds']=time.perf_counter()-row.pop('started');row['status']=response.status_code
  if response.status_code==200:
   body=response.json()
   with (a.output/'http-completions.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps({'role':current_role,'model':body.get('model'),'content':((body.get('choices') or [{}])[0].get('message') or {}).get('content'),'usage':body.get('usage')},ensure_ascii=False)+'\n')
   row['usage']=body.get('usage');row['server_model']=body.get('model');tokens+=(body.get('usage') or {}).get('total_tokens',0)
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
 if a.resume_from and a.mode!='simulated':p.error('Resume supports simulated users only')
 resumed_rows=[json.loads(line) for line in (a.resume_from/'turns.jsonl').read_text(encoding='utf-8').splitlines()] if a.resume_from else []
 verdicts=[]
 for scene in chosen:
  saved=[r for r in resumed_rows if r['scenario']==scene['id']]
  if a.resume_from and not saved:p.error('No saved dialogue for '+scene['id'])
  task=saved[0]['task_id'] if saved else create_task('dialogue-acceptance');context={};transcript=[];previous={};metric_id=None;failed=[];decisions=[];last=None;paused=False;stalled=0;retry=None
  if saved:
   persisted=get_task(task);last=persisted['intake_result'];context=last['input_context'];previous=deepcopy(last['request']['fields'])
   for r in saved:transcript.extend([{'role':'user','text':r['input']},{'role':'assistant','text':r['actual_display']}])
   with PostgresSaver.from_conn_string(make_conninfo(**db_config())) as saver:
    checkpoint=build_intake_graph(client,model,prompt,saver).get_state({'configurable':{'thread_id':task}})
    if checkpoint.next==('process',):retry=checkpoint.values
   manifest.setdefault('resumed_tasks',{})[scene['id']]={'task_id':task,'parent':str(a.resume_from),'parent_turns':len(saved),'checkpoint_retry':bool(retry),'parent_sha256':hashlib.sha256((a.resume_from/'turns.jsonl').read_bytes()).hexdigest()}
   (a.output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
   metric_id=next((g['id'] for g in previous['target_performance'] if '氧化诱导期' in (g.get('value') or '')),None)
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
    text=None
    if retry and n==0:
     text=retry['user_text'];context=retry.get('input_context') or {};user_decisions=[]
    elif a.mode=='fixed':
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
    with patch('lapis_intake.generate_recommendations',side_effect=recommended):out=run_intake_turn(task,text,'dialogue-acceptance',client,model,prompt,retry['operation_id'] if retry and n==0 else str(uuid4()),input_context=context)
    last=out['result'];context=last['input_context'];previous=last['request']['fields'];view=last['view']['text'];transcript.extend([{'role':'user','text':text},{'role':'assistant','text':view}])
    goals=previous['target_performance'];fresh_errors=[]
    if '氧化诱导期' in text and not text.startswith(('限制','假设')):
     found=[g for g in goals if '氧化诱导期' in (g.get('value') or '')]
     if not found:fresh_errors.append('明确指标未进入当前规约')
     elif not metric_id:metric_id=found[0]['id']
    if metric_id and not any(g.get('id')==metric_id and '氧化诱导期' in (g.get('value') or '') for g in goals):fresh_errors.append('无意丢失已选指标')
    if paused and not any(i.get('kind')=='pause' for i in (last.get('interpretation') or {}).get('intents',[])) and 'pause' not in (last.get('interpretation') or {}).get('actions',[]) and text not in {'暂停','先不确认','暂不确认'}:fresh_errors.append('模拟器暂停标记与实际发言不一致，需要语义复核')
    elif paused and (last['intake_status']!='paused' or last.get('next_question') is not None or last['ready_for_design']):fresh_errors.append('用户暂停未被产品正确执行')
    if last['ready_for_design'] and not last.get('confirmation_event'):fresh_errors.append('没有确认事件却进入研究设计')
    if text=='没有约束条件' and not any(c.get('status')=='specified' and c.get('strength')=='hard' for c in (old.get('constraints') or [])) and not any(c.get('status')=='none' for c in previous['constraints']):fresh_errors.append('明确无约束没有记录为none')
    if text=='没有不研究的' and previous['research_scope'].get('value')==text:fresh_errors.append('正式范围脱离已有上下文')
    if text.startswith(('假设：','限制：','拟研究：')) and old and previous!=old:fresh_errors.append('复制提案改变研究字段')
    old_ids={g['id']:g for g in old.get('target_performance',[]) if g.get('status')=='specified'}
    new_ids={g.get('id') for g in goals}
    edits=[d['operation'] for d in last.get('operation_decisions',[]) if d.get('decision')=='accepted' and d['operation']['field']=='target_performance' and d['operation']['action'] in {'remove','replace'}]
    for gid in old_ids.keys()-new_ids:
     if not any(e['action']=='replace' or e.get('target_id')==gid or e.get('target_value')==old_ids[gid].get('value') for e in edits):fresh_errors.append('目标条目消失但没有接受的撤回/替换操作')
    diff={f:{'before':old.get(f),'after':v} for f,v in previous.items() if old.get(f)!=v}
    stalled=stalled+1 if not diff and not last['ready_for_design'] and ('confirm' in (last.get('interpretation') or {}).get('actions',[]) or last['intake_status']=='needs_clarification') else 0
    if stalled>=3:fresh_errors.append('连续三轮核对或澄清仍未推进，停止本例并复核')
    row={'run_id':run,'scenario':scene['id'],'task_id':task,'turn':n+1+len(saved),'input':text,'actual_display':view,'interpretation':last.get('interpretation'),'operation_decisions':last.get('operation_decisions'),'field_diff':diff,'request':last['request'],'result_status':last['intake_status'],'context':context,'seconds':time.perf_counter()-start,'errors':fresh_errors,'user_decisions':user_decisions,'mode':('hybrid_fixed_recommendation_real_extraction' if a.mode=='fixed' and scene['id']=='long_additive' else a.mode)}
    with (a.output/'turns.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(row,ensure_ascii=False,default=str)+'\n')
    with (a.output/'actual-display.md').open('a',encoding='utf-8') as f:f.write('\n## '+scene['id']+' '+str(n+1)+'\n你> '+text+'\n```text\n'+view+'```\n')
    print(scene['id'],n+1,last['intake_status'],fresh_errors,flush=True);failed.extend(fresh_errors)
    if fresh_errors:break
    if a.mode=='simulated' and (last['ready_for_design'] or paused or scene['terminal']=='blocked' and n>=1 and any(i.get('kind')=='conflict' for i in last['blocking_issues'])):break
   good=last and not failed and (last['ready_for_design'] or scene['terminal']=='pause' and paused and last['intake_status']=='paused' and last.get('next_question') is None or scene['terminal']=='blocked' and not last['ready_for_design'] and any(i.get('kind')=='conflict' for i in last['blocking_issues']))
   if scene['id']=='long_additive' and a.mode=='fixed' and not metric_id:good=False;failed.append('未实际验证指标采用及保留')
   if good and last['ready_for_design']:
    child=subprocess.run([sys.executable,'-X','utf8',str(ROOT/'lapis.py'),'chat','--task-id',task],input='/show\n/exit\n',text=True,encoding='utf-8',capture_output=True,timeout=30)
    good=child.returncode==0 and all(x+'：' in child.stdout for x in LABELS.values()) and (not metric_id or '氧化诱导期' in child.stdout)
    (a.output/(scene['id']+'-resume.txt')).write_text(child.stdout+child.stderr,encoding='utf-8')
   verdicts.append({'scenario':scene['id'],'task_id':task,'verdict':'passed' if good else 'failed','errors':failed,'active_request_version':get_task(task)['active_request_version']})
  except Exception as e:
   error={'scenario':scene['id'],'verdict':'error','error_type':type(e).__name__,'error':str(e).replace(key,'[REDACTED]')[:400],'failed_input':locals().get('text'),'turn':locals().get('n',-1)+1,'task_id':task}
   verdicts.append(error)
   with (a.output/'failures.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(error,ensure_ascii=False)+'\n')
  with (a.output/'private-user-decisions.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps({'scenario':scene['id'],'background':scene['background'],'decisions':decisions},ensure_ascii=False)+'\n')
  summary={'run_id':run,'verdicts':verdicts,'calls':calls,'total_tokens':tokens,'budget_stops':budget_stops,'semantic_review':'requires_human_review','not_scientific_evidence':True}
  (a.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
  if len(calls)>=a.max_calls or tokens>=a.max_tokens:break
 print(json.dumps(summary,ensure_ascii=False,default=str),flush=True)
 return 0 if verdicts and all(v['verdict']=='passed' for v in verdicts) else 1

if __name__=='__main__':raise SystemExit(main())
