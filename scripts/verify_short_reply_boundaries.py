"""Controlled recommendation fixture plus real model extraction; not full live generation."""
import os,sys,json,subprocess,io
from pathlib import Path
from copy import deepcopy
from contextlib import redirect_stdout
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import instructor
from openai import OpenAI
from lapis_intake import new_state,extract,handle_turn,SYSTEM_PROMPT
from lapis_core import digest
from lapis import render_intake_result
from test_lapis_review import advice
from test_lapis_proposals import proposal
from unittest.mock import patch
state=new_state();state['domain']='materials_application';body=proposal()
first=body['options'][0]
first.update(label='包装薄膜保护功能研究',reason='测试用途差异',assumptions=['仅为合成推荐'],limitations=['没有科研证据'])
for key,value in [('application','包装薄膜'),('material_function','保护功能')]:first['fields'][key]['value']=value
second=deepcopy(first);second['label']='表面涂层保护功能研究';second['fields']['application']['value']='表面涂层';body['options']=[first,second]
r=advice(state,body)
client=instructor.from_openai(OpenAI(api_key=os.getenv('LAPIS_API_KEY') or os.environ['DEEPSEEK_API_KEY'],base_url=os.getenv('LAPIS_BASE_URL','https://api.deepseek.com'),timeout=40,max_retries=0),mode=instructor.Mode.JSON)
model=os.getenv('LAPIS_MODEL','deepseek-flash');text='保护作用吧'
value=extract(client,model,state,text)
with patch('lapis_intake.extract',return_value=value):result=handle_turn(None,model,state,text,r['input_context'])
buf=io.StringIO()
with redirect_stdout(buf):render_intake_result(result)
good=not state['proposals'] and state['fields']['application']['status']=='unknown' and bool(value.clarification_question) and all(x in value.clarification_question for x in ['包装','涂层'])
record={'kind':'controlled_recommendation_real_extraction','data_origin':'synthetic','commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'prompt_hash':digest(SYSTEM_PROMPT),'model':model,'input':text,'recommendation_fixture':state['recommendation_set'],'extraction':value.model_dump(),'result':result,'display':buf.getvalue(),'verdict':'passed' if good else 'failed'}
(ROOT/'reports/2026-10-09-short-reply-boundary-real.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
print(record['verdict']);print(buf.getvalue());raise SystemExit(0 if good else 1)
