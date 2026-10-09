"""Complete software-only dialogue scenarios; no simulated-user oracle leakage."""
import json
from pathlib import Path
from copy import deepcopy
import unittest
from unittest.mock import patch
from lapis_intake import Extraction, Intent, Update, handle_turn, new_state
from lapis_contract import validate_current_request
from test_lapis_review import advice, select
from test_lapis_proposals import proposal

class CompleteDialogueTest(unittest.TestCase):
    def say(self,state,text,e,context=None):
        with patch('lapis_intake.extract',return_value=e):return handle_turn(None,'software-double',state,text,context)

    def test_ten_complete_contexts(self):
        scenes=json.loads(Path('tests/fixtures/intake_dialogues/development.json').read_text(encoding='utf-8'))
        for scene in scenes:
            with self.subTest(scene=scene['id']):
                state=new_state();obj=scene['material'];text='我想研究'+obj+'的性能。'
                first=self.say(state,text,Extraction(updates=[Update(field='research_object',status='specified',value=obj,quote=obj),Update(field='purpose',status='specified',value='考察材料表现',quote=text)]))
                self.assertFalse(first['ready_for_design'])
                text='用于器件材料，关注稳定性；条件还不知道。'
                e=Extraction(domain='materials_application',domain_quote='用于器件材料',updates=[Update(field='application',status='specified',value='器件材料',quote='用于器件材料',change_relation='refinement'),Update(field='target_performance',status='specified',value='稳定性',direction='考察',quote='稳定性'),Update(field='work_conditions',status='open',quote='条件还不知道')])
                r=self.say(state,text,e,first['input_context'])
                self.assertEqual(r['intake_status'],'needs_confirmation')
                self.assertEqual(state['fields']['work_conditions']['status'],'open')
                compatible=proposal()
                for field,entry in state['fields'].items():
                    compatible['options'][0]['fields'][field]=[{k:e[k] for k in ('status','value','direction','strength') if k in e} for e in entry] if isinstance(entry,list) else {k:entry[k] for k in ('status','value') if k in entry}
                if scene['id']=='multisentence':
                    text='同时关注离子传输，不能使用含氟添加剂，尽量低成本'
                    e=Extraction(updates=[Update(field='target_performance',status='specified',value='离子传输',direction='比较',quote='离子传输'),Update(field='constraints',status='specified',value='不能使用含氟添加剂',strength='hard',quote='不能使用含氟添加剂'),Update(field='constraints',status='specified',value='尽量低成本',strength='preference',quote='尽量低成本')])
                    r=self.say(state,text,e,r['input_context']);self.assertEqual(len(state['fields']['target_performance']),2)
                    self.assertEqual({c.get('strength') for c in state['fields']['constraints']},{'hard','preference'})
                elif scene['id']=='multi_goal_edit':
                    r=self.say(state,'新增导热关注点',Extraction(updates=[Update(field='target_performance',status='specified',value='导热',direction='考察',quote='导热')]),r['input_context'])
                    goal=state['fields']['target_performance'][0]
                    e=Extraction(intents=[Intent(id='E',kind='edit',quote='撤回稳定性',target_refs=[goal['id']])],updates=[Update(field='target_performance',intent_ref='E',status='none',quote='撤回稳定性',action='remove',target_id=goal['id'])])
                    r=self.say(state,'撤回稳定性',e,r['input_context']);self.assertEqual([g['value'] for g in state['fields']['target_performance']],['导热'])
                elif scene['id']=='unknown_delegate':
                    r=self.say(state,'不能使用有毒溶剂',Extraction(updates=[Update(field='constraints',status='specified',value='不能使用有毒溶剂',strength='hard',quote='不能使用有毒溶剂')]),r['input_context'])
                    hard=deepcopy(state['fields']['constraints']);state['current_question']={'id':'C','fields':['constraints']}
                    e=Extraction(intents=[Intent(id='I',kind='delegate',quote='随便你')],updates=[Update(field='constraints',intent_ref='I',status='none',quote='随便你',action='replace')])
                    r=self.say(state,'随便你',e,r['input_context']);self.assertEqual(state['fields']['constraints'],hard)
                elif scene['id']=='context_change':
                    r=self.say(state,'改为器件表面材料',Extraction(updates=[Update(field='application',status='specified',value='器件表面材料',quote='器件表面材料',change_relation='refinement')]),r['input_context'])
                    self.assertFalse(any(g.get('needs_review') for g in state['fields']['target_performance']))
                    r=self.say(state,'改到室内吸附用途',Extraction(domain='materials_application',domain_quote='室内吸附',updates=[Update(field='application',status='specified',value='室内吸附',quote='室内吸附',change_relation='switch')]),r['input_context'])
                    self.assertTrue(any(g.get('needs_review') for g in state['fields']['target_performance']))
                    r=self.say(state,'目标、功能和条件重新核对后仍沿用',Extraction(actions=['reaffirm'],reaffirm_fields=['target_performance','material_function','work_conditions','constraints']),r['input_context'])
                elif scene['id']=='indicator':
                    r=self.say(state,'也记录氧化诱导期',Extraction(updates=[Update(field='target_performance',status='specified',value='氧化诱导期',direction='考察',quote='氧化诱导期')]),r['input_context'])
                    self.assertIn('氧化诱导期',[g['value'] for g in state['fields']['target_performance']])
                elif scene['id']=='quotation':
                    original=deepcopy(state['fields']);r=advice(state,compatible);r=select(state,r['input_context'])
                    text='限制：'+'；'.join(state['recommendation_set']['options'][0]['limitations'])
                    before=deepcopy(state['fields']['constraints']);r=handle_turn(None,'software-double',state,text,r['input_context'])
                    self.assertEqual(state['fields']['constraints'],before)
                    r=self.say(state,'低成本是偏好',Extraction(updates=[Update(field='constraints',status='specified',value='低成本',strength='preference',quote='低成本是偏好')]),r['input_context'])
                elif scene['id']=='beginner':
                    r=advice(state,compatible);r=select(state,r['input_context']);self.assertFalse(r['ready_for_design'])
                elif scene['id']=='long_additive':
                    r=self.say(state,'新增氧化诱导期',Extraction(updates=[Update(field='target_performance',status='specified',value='氧化诱导期',direction='比较',quote='氧化诱导期')]),r['input_context'])
                    before=deepcopy(state['fields']['target_performance']);state['current_question']={'id':'S','fields':['research_scope']}
                    e=Extraction(intents=[Intent(id='I',kind='answer',quote='都包含',question_ref='S')],updates=[Update(field='research_scope',intent_ref='I',status='specified',value='都包含',quote='都包含',scope_mode='current_scope'),Update(field='target_performance',intent_ref='I',status='specified',value='其他目标',direction='考察',quote='都包含',action='replace')])
                    r=self.say(state,'都包含',e,r['input_context']);self.assertEqual(state['fields']['target_performance'],before)
                if scene['terminal']=='pause':
                    r=handle_turn(None,'software-double',state,'先不确认',r['input_context']);self.assertFalse(r['ready_for_design'])
                elif scene['terminal']=='blocked':
                    text='必须使用FEC，不能使用FEC'
                    e=Extraction(updates=[Update(field='constraints',status='specified',value='必须使用FEC',strength='hard',quote='必须使用FEC'),Update(field='constraints',status='specified',value='不能使用FEC',strength='hard',quote='不能使用FEC')])
                    r=self.say(state,text,e,r['input_context']);r=handle_turn(None,'software-double',state,'确认',r['input_context']);self.assertFalse(r['ready_for_design']);self.assertTrue(r['blocking_issues'])
                else:
                    r=handle_turn(None,'software-double',state,'确认',r['input_context']);validate_current_request(r['request']);self.assertTrue(r['ready_for_design'])

    def test_long_copy_scope_trace_keeps_indicator(self):
        state=new_state();r=advice(state);r=select(state,r['input_context'])
        opt=state['recommendation_set']['options'][0]
        for text in ['拟研究：'+opt['fields']['purpose']['value'],'假设：'+'；'.join(opt['assumptions']),'限制：'+'；'.join(opt['limitations'])]:
            r=handle_turn(None,'software-double',state,text,r['input_context']);self.assertFalse(state['reference_notes'])
        text='氧化诱导期';r=self.say(state,text,Extraction(intents=[Intent(id='G',kind='inform',quote=text)],updates=[Update(field='target_performance',intent_ref='G',status='specified',value=text,direction='比较',quote=text)]),r['input_context'])
        metric=next(g['id'] for g in state['fields']['target_performance'] if g.get('value')==text)
        for text in ['都研究','都包含','没有不研究的']:
            q='scope-q';state['current_question']={'id':q,'fields':['research_scope']}
            e=Extraction(intents=[Intent(id='S',kind='answer',quote=text,question_ref=q)],updates=[Update(field='research_scope',intent_ref='S',status='specified',value=text,quote=text,scope_mode='no_extra_exclusions'),Update(field='target_performance',intent_ref='S',status='specified',value='抗氧化性',direction='考察',action='replace',quote=text)])
            r=self.say(state,text,e,r['input_context'])
            self.assertTrue(any(g['id']==metric and g['value']=='氧化诱导期' for g in state['fields']['target_performance']))
            self.assertNotEqual(state['fields']['research_scope']['value'],text)
            self.assertFalse(any(g.get('needs_review') for g in state['fields']['target_performance']))
        r=handle_turn(None,'software-double',state,'确认',r['input_context']);self.assertTrue(r['ready_for_design'])
        self.assertIn('氧化诱导期',[g['value'] for g in r['request']['fields']['target_performance']])

    def test_none_answer_does_not_erase_existing_hard_constraint(self):
        state=new_state();r=advice(state);r=select(state,r['input_context'])
        text='不能使用有毒溶剂';r=self.say(state,text,Extraction(updates=[Update(field='constraints',status='specified',value=text,strength='hard',quote=text)]),r['input_context'])
        hard=next(c for c in state['fields']['constraints'] if c.get('value')==text);state['current_question']={'id':'CQ','fields':['constraints']}
        e=Extraction(intents=[Intent(id='I',kind='answer',quote='随便你',question_ref='CQ')],updates=[Update(field='constraints',intent_ref='I',status='none',quote='随便你',target_id=hard['id'],action='update')])
        self.say(state,'随便你',e,r['input_context'])
        self.assertTrue(any(c.get('id')==hard['id'] and c.get('status')=='specified' for c in state['fields']['constraints']))

if __name__=='__main__':unittest.main()
