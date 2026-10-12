"""Zero-fee controlled audit reproduction and complete dialogue; never science evidence."""
import json,sys
from pathlib import Path
from copy import deepcopy
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lapis_intake import Extraction,Intent,Update,handle_turn,new_state
from lapis_contract import validate_current_request
from test_lapis_boundaries import BoundaryTest
from test_lapis_review import advice,select

def main():
    records=[]
    for ref in ('A',None):
        state,r=BoundaryTest().seed();before=deepcopy(state['fields']);goal=before['target_performance'][0]
        e=Extraction(intents=[Intent(id='A',kind='answer',quote='都包含',question_ref='scope-Q')],updates=[Update(field='target_performance',status='none',quote='都包含',action='remove',target_id=goal['id'],intent_ref=ref)])
        with patch('lapis_intake.extract',return_value=e):out=handle_turn(None,'controlled-no-model',state,'都包含',r['input_context'])
        assert state['fields']==before and out['operation_decisions'][0]['decision']=='not_applied'
        records.append({'case':'audit-linked-answer' if ref else 'audit-missing-association','fields_preserved':True,'decisions':out['operation_decisions'],'actual_display':out['view']['text']})
    state=new_state();r=advice(state);rows=[]
    def save(text,result):
        rows.append({'input':text,'actual_display':result['view']['text'],'interpretation':result.get('interpretation'),'operations':result.get('operation_decisions'),'request':deepcopy(result['request']),'status':result['intake_status']})
    save('请推荐',r);name=state['recommendation_set']['options'][0]['label'];option=state['recommendation_set']['options'][0]
    with patch('lapis_intake.extract',return_value=Extraction(actions=['select'],selected_option=1,intents=[Intent(id='A',kind='adopt',quote=name,target_refs=[option['id']])])) as model:r=handle_turn(None,'controlled-no-model',state,name,r['input_context'])
    model.assert_called_once();save(name,r)
    text='限制：'+'；'.join(option['limitations']);before=deepcopy(state['fields']);r=handle_turn(None,'controlled-no-model',state,text,r['input_context']);assert state['fields']==before;save(text,r)
    text='新增氧化诱导期，原关注点保留'
    with patch('lapis_intake.extract',return_value=Extraction(intents=[Intent(id='G',kind='inform',quote=text)],updates=[Update(field='target_performance',status='specified',value='氧化诱导期',direction='考察',quote='氧化诱导期',intent_ref='G',basis_refs=['target_performance'])])):r=handle_turn(None,'controlled-no-model',state,text,r['input_context'])
    save(text,r);metric=next(g['id'] for g in state['fields']['target_performance'] if g.get('value')=='氧化诱导期')
    for text in ('都包含','没有额外排除项'):
        state['current_question']={'id':'scope-Q','fields':['research_scope']}
        e=Extraction(intents=[Intent(id='S',kind='answer',quote=text,question_ref='scope-Q')],updates=[Update(field='research_scope',status='specified',value=text,quote=text,intent_ref='S',scope_mode='current_scope',basis_refs=['research_scope']),Update(field='target_performance',status='none',quote=text,action='remove',target_id=metric)])
        with patch('lapis_intake.extract',return_value=e):r=handle_turn(None,'controlled-no-model',state,text,r['input_context'])
        assert any(g.get('id')==metric for g in state['fields']['target_performance']);save(text,r)
    r=handle_turn(None,'controlled-no-model',state,'确认',r['input_context']);validate_current_request(r['request']);assert r['confirmation_event'];save('确认',r)
    result={'mode':'controlled-software-new-run','model_calls':0,'database_writes':False,'not_scientific_evidence':True,'audit_reproductions':records,'complete_dialogue':rows,'confirmed':True,'indicator_preserved':True}
    out=ROOT/'reports/2026-10-12-boundary-software-replay.json';out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print('Audit controls and complete software dialogue passed; zero model calls')

if __name__=='__main__':main()
