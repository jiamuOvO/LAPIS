"""Real synthetic short-reply acceptance; no production task writes."""
import os,sys,json,io,subprocess,hashlib,time
from pathlib import Path
from contextlib import redirect_stdout
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import instructor
from openai import OpenAI
import lapis_intake
from unittest.mock import patch
from lapis_intake import SYSTEM_PROMPT,PROPOSAL_PROMPT,LABELS
from lapis_core import digest
from lapis_graph import run_intake_turn
from lapis_store import create_task,get_task
from lapis import render_intake_result
assert os.getenv('LAPIS_TEST_PG')=='1' and os.getenv('LAPIS_DB_NAME')=='lapis_test'
client=instructor.from_openai(OpenAI(api_key=os.getenv('LAPIS_API_KEY') or os.environ['DEEPSEEK_API_KEY'],base_url=os.getenv('LAPIS_BASE_URL','https://api.deepseek.com'),timeout=40,max_retries=0),mode=instructor.Mode.JSON)
model=os.getenv('LAPIS_MODEL','deepseek-flash');prompt=digest({'extract':SYSTEM_PROMPT,'proposal':PROPOSAL_PROMPT})
run=str(uuid4());baseline=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
hashes={f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in ('lapis_intake.py','lapis.py','lapis_contract.py')}
outfile=ROOT/'reports/2026-10-09-short-reply-real.jsonl';views=outfile.with_suffix('.views.md')
for case,intent,short,application,role,scope in [
 ('furan','我想要研究呋喃基在日常生活中的应用','抗氧化作用吧','先研究包装材料中的抗氧化应用','拟承担延缓氧化的作用，只是待验证意图','先比较含呋喃基的候选材料，只研究抗氧化，不研究抗菌或器件'),
 ('silica','我想研究二氧化硅在生活中的应用','耐磨作用吧','先研究表面涂层的耐磨应用','拟提高涂层耐磨性，只是待验证意图','比较含二氧化硅的涂层候选，只研究耐磨，不研究其他功能')]:
 task=create_task('short-reply-synthetic');context={}
 def step(text):
  global context
  extractions=[]
  original=lapis_intake.extract
  def traced(*args,**kwargs):
   value=original(*args,**kwargs);extractions.append(value.model_dump());return value
  start=time.perf_counter()
  with patch('lapis_intake.extract',side_effect=traced):result=run_intake_turn(task,text,'short-reply-synthetic',client,model,prompt,str(uuid4()),input_context=context)['result'];context=result['input_context']
  buf=io.StringIO()
  with redirect_stdout(buf):render_intake_result(result)
  rec={'run':run,'case':case,'task_id':task,'input':text,'extractions':extractions,'result':result,'display':buf.getvalue(),'commit':baseline,'code_hashes':hashes,'model':model,'prompt_hash':prompt,'data_origin':'synthetic_real_model','seconds':time.perf_counter()-start}
  with outfile.open('a',encoding='utf-8') as f:f.write(json.dumps(rec,ensure_ascii=False)+'\n')
  with views.open('a',encoding='utf-8') as f:f.write('\n## '+case+'\n你> '+text+'\n```text\n'+buf.getvalue()+'```\n')
  print(case,text,result['intake_status'],flush=True);return result
 try:
  step(intent);step('你帮我推荐吧');r=step(short)
  goals=r['request']['fields']['target_performance'];assert len([g for g in goals if g['status']=='specified'])==1,'short answer must keep one goal'
  assert not r['ready_for_design']
  assert r['request']['fields']['purpose']['status']=='specified','short answer must resolve purpose'
  assert not any(i.get('field')=='purpose' for i in r['blocking_issues']),'old purpose ambiguity must resolve'
  step('可以下一步了吗');r=step(application)
  answers={'purpose':'先探索候选材料是否适合这个功能，不假定已经有效。','application':application,'research_object':'研究刚才提到的材料候选，不限定具体配方。','research_scope':scope,'material_function':role,'target_performance':short,'work_conditions':'条件暂不设置，研究设计阶段确定。','constraints':'本轮没有预设约束。'}
  for n in range(7):
   if r['intake_status']=='needs_confirmation':break
   state=get_task(task)['intake_state'];field=state.get('asked_field');r=step(answers.get(field,'请明确告诉我还缺哪项研究意图。'))
  r=step('可以下一步了吗');assert r['intake_status']=='needs_confirmation','must present complete current review';assert not r.get('confirmation_event')
  r=step('确认');assert r['ready_for_design'] and get_task(task)['active_request_version'] is not None
  child=subprocess.run([sys.executable,'-X','utf8',str(ROOT/'lapis.py'),'chat','--task-id',task],input='/show\n/exit\n',capture_output=True,text=True,encoding='utf-8',timeout=30)
  assert child.returncode==0 and all(x+'：' in child.stdout for x in LABELS.values())
  with views.open('a',encoding='utf-8') as f:f.write('\n## '+case+' 新进程恢复\n```text\n'+child.stdout+'```\n')
  verdict={'run':run,'case':case,'verdict':'passed','task_id':task,'resume_no_model_call':True}
 except Exception as e:verdict={'run':run,'case':case,'verdict':'failed','error':str(e),'task_id':task}
 with outfile.open('a',encoding='utf-8') as f:f.write(json.dumps(verdict,ensure_ascii=False)+'\n')
 print(verdict,flush=True)
