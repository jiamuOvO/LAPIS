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


if __name__=='__main__':unittest.main()
