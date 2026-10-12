"""Independent audit regressions. No repair of intentionally malformed fixtures."""
import unittest
from copy import deepcopy
from unittest.mock import patch
from pydantic import ValidationError
from lapis_intake import Extraction,Intent,Update,handle_turn,new_state,contextual_reply
import test_lapis_semantics as semantic_tests
from test_lapis_review import advice,select


class BoundaryTest(unittest.TestCase):
    def seed(self):
        state,r=semantic_tests.SemanticTest().base();goal=state['fields']['target_performance'][0]
        text='把稳定性改为氧化诱导期'
        e=Extraction(actions=['edit'],intents=[Intent(id='E',kind='edit',quote=text,target_refs=[goal['id']])],updates=[Update(field='target_performance',status='specified',value='氧化诱导期',direction='比较',quote='氧化诱导期',action='update',intent_ref='E',target_id=goal['id'],basis_refs=[goal['id']])])
        with patch('lapis_intake.extract',return_value=e):r=handle_turn(None,'controlled',state,text,r['input_context'])
        self.assertEqual(state['fields']['target_performance'][0]['value'],'氧化诱导期')
        state['current_question']={'id':'scope-Q','fields':['research_scope']}
        return state,r

    def apply(self,state,r,text,e):
        with patch('lapis_intake.extract',return_value=e):return handle_turn(None,'controlled',state,text,r['input_context'])

    def test_audit_missing_null_empty_invalid_and_wrong_intent_cannot_remove(self):
        for ref in ('omitted',None,'','missing','A'):
            for kind in ('answer','quote','inform','progress','confirm'):
                with self.subTest(ref=ref,kind=kind):
                    state,r=self.seed();before=deepcopy(state['fields']);goal=before['target_performance'][0]
                    data=dict(field='target_performance',status='none',quote='都包含',action='remove',target_id=goal['id'],basis_refs=[goal['id']])
                    if ref!='omitted':data['intent_ref']=ref
                    e=Extraction(intents=[Intent(id='A',kind=kind,quote='都包含',question_ref='scope-Q',target_refs=[goal['id']])],updates=[Update(**data)])
                    out=self.apply(state,r,'都包含',e)
                    self.assertEqual(state['fields'],before);self.assertEqual(out['operation_decisions'][0]['decision'],'not_applied')
                    self.assertFalse(state['issues'])

    def test_unlinked_item_is_rejected_even_with_another_valid_edit(self):
        state,r=self.seed();goal=state['fields']['target_performance'][0];before=deepcopy(state['fields'])
        e=Extraction(intents=[Intent(id='E',kind='edit',quote='范围不变',target_refs=['research_scope'])],updates=[Update(field='target_performance',status='none',quote='都包含',action='remove',target_id=goal['id'],basis_refs=[goal['id']])])
        out=self.apply(state,r,'范围不变，都包含',e);self.assertEqual(state['fields'],before);self.assertEqual(out['operation_decisions'][0]['decision'],'not_applied')

    def test_clear_replace_scalar_and_constraint_strength_require_link(self):
        for field,action,status in [('target_performance','replace','none'),('target_performance','update','open'),('purpose','update','open'),('constraints','update','specified')]:
            with self.subTest(field=field,action=action):
                state,r=self.seed();before=deepcopy(state['fields']);value=before[field];entry=value[0] if isinstance(value,list) else value
                data=dict(field=field,status=status,quote='都包含',action=action)
                if isinstance(value,list):data.update(target_id=entry['id'])
                if field=='constraints':data['strength']='preference'
                out=self.apply(state,r,'都包含',Extraction(updates=[Update(**data)]));self.assertEqual(state['fields'],before);self.assertEqual(out['operation_decisions'][0]['decision'],'not_applied')

    def test_wrong_target_name_refs_and_basis_are_rejected(self):
        for defect in ('name','refs','basis','absent_basis'):
            with self.subTest(defect=defect):
                state,r=self.seed();before=deepcopy(state['fields']);a,b=before['target_performance'];text='撤回氧化诱导期'
                intent=Intent(id='E',kind='edit',quote=text,target_refs=[b['id'] if defect=='refs' else a['id']])
                update=Update(field='target_performance',status='none',quote=text,action='remove',intent_ref='E',target_id=a['id'],target_value=b['value'] if defect=='name' else None,basis_refs=[] if defect=='absent_basis' else [b['id'] if defect=='basis' else a['id']])
                out=self.apply(state,r,text,Extraction(intents=[intent],updates=[update]));self.assertEqual(state['fields'],before);self.assertEqual(out['operation_decisions'][0]['decision'],'not_applied')

    def test_duplicate_or_empty_intent_id_fails_contract_without_state_change(self):
        for ids in (['E','E'],['']):
            with self.assertRaises(ValidationError):Extraction(intents=[Intent(id=i,kind='edit',quote='撤回') for i in ids])

    def test_current_model_validation_requests_format_repair_before_merge(self):
        state,r=self.seed();goal=state['fields']['target_performance'][0]
        data={'updates':[{'field':'target_performance','status':'none','quote':'撤回氧化诱导期','action':'remove','target_id':goal['id'],'intent_ref':'E'}]}
        with self.assertRaises(ValidationError):Extraction.model_validate(data,context={'current_model_output':True,'fields':state['fields']})
        # Historical decoding can read it; shared execution guard still refuses it.
        historical=Extraction.model_validate(data)
        before=deepcopy(state['fields']);out=self.apply(state,r,'撤回氧化诱导期',historical)
        self.assertEqual(state['fields'],before);self.assertEqual(out['operation_decisions'][0]['decision'],'not_applied')

    def test_native_instructor_forwards_context_and_repairs_bad_binding_offline(self):
        import json,httpx,instructor
        from openai import OpenAI
        from lapis_intake import extract
        state,r=self.seed();goal=state['fields']['target_performance'][0];text='撤回氧化诱导期';bodies=[]
        def respond(request):
            bodies.append(json.loads(request.content))
            update={'field':'target_performance','status':'none','quote':text,'action':'remove','target_id':goal['id'],'intent_ref':'E'}
            if len(bodies)>1:update['basis_refs']=[goal['id']]
            result={'intents':[{'id':'E','kind':'edit','quote':text,'target_refs':[goal['id']]}],'updates':[update]}
            return httpx.Response(200,json={'id':'offline','object':'chat.completion','created':0,'model':'offline','choices':[{'index':0,'message':{'role':'assistant','content':json.dumps(result,ensure_ascii=False)},'finish_reason':'stop'}]})
        client=instructor.from_openai(OpenAI(api_key='offline-only',base_url='https://offline.invalid',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(respond))),mode=instructor.Mode.JSON)
        result=extract(client,'offline',state,text)
        self.assertEqual(len(bodies),2);self.assertEqual(result.updates[0].basis_refs,[goal['id']])
        self.assertTrue(all('context' not in b and 'validation_context' not in b for b in bodies))

    def test_explicit_named_removal_and_whole_list_replace_work(self):
        state,r=self.seed();text='撤回氧化诱导期，其余保留'
        e=Extraction(actions=['edit'],intents=[Intent(id='E',kind='edit',quote=text,target_refs=['氧化诱导期'])],updates=[Update(field='target_performance',status='none',quote='撤回氧化诱导期',action='remove',target_value='氧化诱导期',intent_ref='E',basis_refs=['target_performance'])])
        out=self.apply(state,r,text,e);self.assertEqual([g['value'] for g in state['fields']['target_performance']],['离子传输'])
        text='整个目标列表只保留稳定性'
        e=Extraction(actions=['edit'],intents=[Intent(id='E',kind='edit',quote=text,target_refs=['target_performance'])],updates=[Update(field='target_performance',status='specified',value='稳定性',direction='考察',quote='稳定性',action='replace',intent_ref='E',basis_refs=['target_performance'])])
        self.apply(state,out,text,e);self.assertEqual([g['value'] for g in state['fields']['target_performance']],['稳定性'])

    def test_post_normalization_remove_does_not_bypass_guard(self):
        state,r=self.seed();before=deepcopy(state['fields']);cost=state['fields']['constraints'][1];text='不考虑成本'
        e=Extraction(updates=[Update(field='constraints',status='specified',value=text,strength='preference',quote=text,target_id=cost['id'])])
        out=self.apply(state,r,text,e);self.assertEqual(state['fields'],before);self.assertEqual(out['operation_decisions'][0]['operation']['action'],'remove');self.assertEqual(out['operation_decisions'][0]['decision'],'not_applied')

    def test_bare_names_route_to_understanding_and_correct_selection(self):
        from test_lapis_proposals import proposal
        for name in ('陶瓷散热基板比较','纤维素涂层耐湿研究'):
            state=new_state();body=proposal();body['options'][0]['label']=name;r=advice(state,body);option=state['recommendation_set']['options'][0]
            self.assertIsNone(contextual_reply(state,name))
            e=Extraction(actions=['select'],selected_option=1,intents=[Intent(id='A',kind='adopt',quote=name,target_refs=[option['id']])])
            with patch('lapis_intake.extract',return_value=e) as model:out=handle_turn(None,'controlled',state,name,r['input_context'])
            model.assert_called_once();self.assertTrue(any(h['status']=='accepted' for h in state['recommendation_history']));self.assertFalse(out['ready_for_design'])
            before=deepcopy(state['fields']);self.apply(state,out,name,e);self.assertEqual(state['fields'],before)

    def test_quote_mixed_requirement_and_multiple_names_do_not_force_quote(self):
        state=new_state();r=advice(state);rec=state['recommendation_set'];limit=rec['options'][0]['limitations'][0]
        self.assertEqual(contextual_reply(state,'限制：'+limit).actions,['quote'])
        for text in ('限制：'+limit+'。把这一项作为硬约束',rec['options'][0]['label']+'，但仅选择功能','引用：'+rec['options'][0]['label']):
            self.assertIsNone(contextual_reply(state,text))
        rec['options'].append(deepcopy(rec['options'][0]));self.assertIsNone(contextual_reply(state,rec['options'][0]['label']))

    def test_wrong_model_guess_on_duplicate_bare_label_does_not_adopt(self):
        from test_lapis_proposals import proposal
        state=new_state();body=proposal();second=deepcopy(body['options'][0]);second['fields']['application']['value']='另一用途';body['options'].append(second)
        r=advice(state,body);before=deepcopy(state['fields']);name=state['recommendation_set']['options'][0]['label']
        out=self.apply(state,r,name,Extraction(actions=['select'],selected_option=1))
        self.assertEqual(state['fields'],before);self.assertFalse(state['proposals'])
        self.assertIn('多个方向',out['next_question'])

    def test_stale_menu_and_partial_choice_do_not_adopt_context(self):
        state=new_state();r=advice(state);name=state['recommendation_set']['options'][0]['label']
        with patch('lapis_intake.extract',return_value=Extraction(actions=['select'],selected_option=1)):
            out=handle_turn(None,'controlled',state,name,{'draft_id':'old','recommendation_ref':r['input_context']['recommendation_ref']})
        self.assertFalse(state['proposals']);self.assertIn('变化',out['notice'])
        with patch('lapis_intake.extract',return_value=Extraction(actions=['select'],selection_mode='partial',selected_option=1)):
            handle_turn(None,'controlled',state,name+'，只选功能',r['input_context'])
        self.assertFalse(state['proposals']);self.assertEqual(state['fields']['application']['status'],'unknown')

    def test_known_requirement_short_unknown_cannot_use_trusted_shortcut(self):
        state,r=self.seed();state['asked_field']='target_performance'
        self.assertIsNone(contextual_reply(state,'不知道'))

    def test_changed_goals_keep_immutable_source_labeled_as_history(self):
        state=new_state();r=advice(state);r=select(state,r['input_context']);original=deepcopy(state['proposals']);goal=state['fields']['target_performance'][0];text='撤回'+goal['value']
        e=Extraction(actions=['edit'],intents=[Intent(id='E',kind='edit',quote=text,target_refs=[goal['id']])],updates=[Update(field='target_performance',status='none',quote=text,action='remove',target_id=goal['id'],intent_ref='E',basis_refs=[goal['id']]),Update(field='target_performance',status='specified',value='新关注点',direction='考察',quote='新关注点')])
        out=self.apply(state,r,text+'，新增新关注点',e)
        self.assertEqual(state['proposals'],original)
        self.assertIn('原始快照',out['view']['text']);self.assertIn('原提案被撤回的内容不再适用',out['view']['text'])
        self.assertNotIn(goal['value'],out['request']['fields']['target_performance'][0]['value'])

    def test_explicit_scope_edit_keeps_exclusions_even_with_current_scope_mode(self):
        state,r=self.seed();text='仅研究材料表现，不验证器件寿命'
        e=Extraction(actions=['edit'],intents=[Intent(id='E',kind='edit',quote=text,target_refs=['research_scope'])],updates=[Update(field='research_scope',status='specified',value=text,quote=text,action='update',intent_ref='E',basis_refs=['research_scope'],target_id='research_scope',scope_mode='current_scope',change_relation='refinement')])
        out=self.apply(state,r,text,e);self.assertEqual(out['request']['fields']['research_scope']['value'],text)

    def test_inform_scope_is_not_silently_normalized_into_old_user_value(self):
        state,r=self.seed();before=deepcopy(state['fields']);text='只研究材料表现，不验证器件寿命'
        e=Extraction(intents=[Intent(id='I',kind='inform',quote=text,target_refs=['research_scope'])],updates=[Update(field='research_scope',status='specified',value=text,quote=text,action='update',intent_ref='I',basis_refs=['research_scope'],target_id='research_scope',scope_mode='current_scope',change_relation='refinement')])
        out=self.apply(state,r,text,e);self.assertEqual(state['fields'],before);self.assertEqual(out['operation_decisions'][0]['decision'],'not_applied')
        self.assertEqual(out['operation_decisions'][0]['operation']['value'],text)
        with self.assertRaises(ValidationError):Extraction.model_validate(e.model_dump(),context={'current_model_output':True,'fields':state['fields'],'state':state,'input':text})

    def test_native_instructor_repairs_wrong_intent_kind_with_shared_guard(self):
        import json,httpx,instructor
        from openai import OpenAI
        from lapis_intake import extract
        state,r=self.seed();text='仅研究材料，不验证器件寿命';bodies=[]
        def respond(request):
            bodies.append(json.loads(request.content))
            result={'intents':[{'id':'I','kind':'inform' if len(bodies)==1 else 'edit','quote':text,'target_refs':['research_scope']}],'updates':[{'field':'research_scope','status':'specified','value':text,'quote':text,'action':'update','intent_ref':'I','basis_refs':['research_scope'],'target_id':'research_scope','scope_mode':'current_scope','change_relation':'refinement'}]}
            return httpx.Response(200,json={'id':'offline','object':'chat.completion','created':0,'model':'offline','choices':[{'index':0,'message':{'role':'assistant','content':json.dumps(result,ensure_ascii=False)},'finish_reason':'stop'}]})
        client=instructor.from_openai(OpenAI(api_key='offline-only',base_url='https://offline.invalid',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(respond))),mode=instructor.Mode.JSON)
        before=deepcopy(state);result=extract(client,'offline',state,text)
        self.assertEqual(len(bodies),2);self.assertEqual(state,before);self.assertEqual(result.intents[0].kind,'edit')

    def test_simulated_bare_name_format_preserves_free_choice(self):
        from scripts.verify_intake_dialogues import UserReply
        context={'visible_bare_names':['方向甲','方向乙']}
        with self.assertRaises(ValidationError):UserReply.model_validate({'text':'我选方向甲'},context=context)
        for name in context['visible_bare_names']:self.assertEqual(UserReply.model_validate({'text':name},context=context).text,name)
        self.assertEqual(UserReply.model_validate({'text':'我想引用方向甲'}).text,'我想引用方向甲')

    def test_scalar_temperature_metadata_is_not_cleared_or_replaced_without_edit(self):
        from lapis_intake import Temperature
        state,r=self.seed();state['fields']['work_conditions']={'status':'specified','value':'298 K','temperatures':[{'value':'298','unit':'K'}],'source':'user','quote':'298 K','turn':1}
        text='仍为298 K'
        e=Extraction(intents=[Intent(id='I',kind='inform',quote=text,target_refs=['work_conditions'])],updates=[Update(field='work_conditions',status='specified',value='298 K',quote=text,intent_ref='I',basis_refs=['work_conditions'])])
        out=self.apply(state,r,text,e);self.assertEqual(state['fields']['work_conditions']['temperatures'],[{'value':'298','unit':'K'}])
        before=deepcopy(state['fields']);e.updates[0].temperatures=[Temperature(value='1000',unit='K')]
        out=self.apply(state,out,text,e);self.assertEqual(state['fields'],before);self.assertEqual(out['operation_decisions'][0]['decision'],'not_applied')

    def test_retired_goal_in_current_purpose_or_scope_blocks_approval_until_edit(self):
        state,r=self.seed();goal=state['fields']['target_performance'][0]
        state['fields']['purpose']['value']='比较氧化诱导期和离子传输'
        state['fields']['research_scope']['value']='本轮包括氧化诱导期'
        text='撤回氧化诱导期'
        e=Extraction(actions=['edit'],intents=[Intent(id='E',kind='edit',quote=text,target_refs=[goal['id']])],updates=[Update(field='target_performance',status='none',quote=text,action='remove',intent_ref='E',target_id=goal['id'],basis_refs=[goal['id']])])
        out=self.apply(state,r,text,e)
        self.assertTrue(all(any(i['field']==f and '已撤回' in i['message'] for i in out['blocking_issues']) for f in ('purpose','research_scope')))
        approved=handle_turn(None,'controlled',state,'确认',out['input_context']);self.assertFalse(approved['ready_for_design'])
        text='目的只比较离子传输，范围只包含离子传输'
        e=Extraction(actions=['edit'],intents=[Intent(id='E',kind='edit',quote=text,target_refs=['purpose','research_scope'])],updates=[Update(field=f,status='specified',value='只比较离子传输',quote=text,action='update',intent_ref='E',basis_refs=[f],target_id=f,change_relation='refinement') for f in ('purpose','research_scope')])
        out=self.apply(state,approved,text,e);self.assertFalse(out['blocking_issues'])
        self.assertTrue(handle_turn(None,'controlled',state,'确认',out['input_context'])['ready_for_design'])


if __name__=='__main__':unittest.main()
