"""Semantic operation regressions including deliberate model mistakes."""
import unittest
from copy import deepcopy
from unittest.mock import patch
from lapis_intake import Extraction, Intent, Update, handle_turn, new_state
from lapis_contract import validate_current_request, validate_research_request_v4
from lapis_semantics import build_context, proposal_echo
from test_lapis_intake import TEXT, full_updates, turn
from test_lapis_review import advice, select

class SemanticTest(unittest.TestCase):
    def base(self):
        state=new_state();r=turn(state,TEXT,full_updates());return state,r
    def send(self,state,text,extraction,context=None):
        with patch('lapis_intake.extract',return_value=extraction):return handle_turn(None,'test',state,text,context)

    def test_scope_answer_cannot_replace_selected_indicator(self):
        state,r=self.base();state['fields']['target_performance'][0]['value']='氧化诱导期'
        state['current_question']={'id':'scope-Q','fields':['research_scope']}
        before=deepcopy(state['fields']['target_performance'])
        e=Extraction(intents=[Intent(id='I',kind='answer',quote='都包含',question_ref='scope-Q')],updates=[Update(field='target_performance',intent_ref='I',status='specified',value='抗氧化性',direction='比较',quote='都包含',action='replace')])
        result=self.send(state,'都包含',e,r['input_context'])
        self.assertEqual(state['fields']['target_performance'],before)
        self.assertEqual(result['operation_decisions'][0]['decision'],'not_applied')

    def test_comparison_answer_only_updates_direction(self):
        state,r=self.base();goal=state['fields']['target_performance'][0];state['current_question']={'id':'goal-Q','fields':['target_performance']}
        e=Extraction(intents=[Intent(id='I',kind='answer',quote='比较吧',question_ref='goal-Q')],updates=[Update(field='target_performance',intent_ref='I',status='specified',direction='比较',quote='比较吧',action='update',target_id=goal['id'],basis_refs=[goal['id']])])
        self.send(state,'比较吧',e,r['input_context'])
        self.assertEqual(len(state['fields']['target_performance']),2)
        self.assertEqual(state['fields']['target_performance'][0]['id'],goal['id'])

    def test_explicit_mixed_edit_is_allowed(self):
        state,r=self.base();goal=state['fields']['target_performance'][0];state['current_question']={'id':'scope-Q','fields':['research_scope']}
        e=Extraction(intents=[Intent(id='S',kind='answer',quote='范围不变',question_ref='scope-Q'),Intent(id='E',kind='edit',quote='撤回稳定性',target_refs=[goal['id']])],updates=[Update(field='target_performance',intent_ref='E',status='none',quote='撤回稳定性',action='remove',target_id=goal['id']),Update(field='research_scope',intent_ref='S',status='specified',quote='范围不变',value=state['fields']['research_scope']['value'],change_relation='restatement')])
        self.send(state,'范围不变，撤回稳定性',e,r['input_context'])
        self.assertEqual([g['value'] for g in state['fields']['target_performance']],['离子传输'])

    def test_exact_multiline_proposal_copy_retains_origin(self):
        state=new_state();r=advice(state);r=select(state,r['input_context']);rec=state['recommendation_set']['options'][0]
        text='拟研究：'+rec['fields']['purpose']['value']+'\n假设：'+'；'.join(rec['assumptions'])+'\n限制：'+'；'.join(rec['limitations'])
        before=deepcopy(state['fields']);refs=deepcopy(state['reference_notes'])
        result=handle_turn(None,'test',state,text,r['input_context'])
        self.assertEqual(state['fields'],before);self.assertEqual(state['reference_notes'],refs)
        self.assertIn('原提案引用',result['notice'])
        self.assertFalse(result['content_changed'])

    def test_only_limitations_do_not_create_constraints(self):
        state=new_state();r=advice(state);r=select(state,r['input_context'])
        text='限制：'+'；'.join(state['recommendation_set']['options'][0]['limitations'])
        before=deepcopy(state['fields']['constraints'])
        handle_turn(None,'test',state,text,r['input_context'])
        self.assertEqual(state['fields']['constraints'],before)
        self.assertFalse(state['reference_notes'])

    def test_compatible_refinement_does_not_reopen_goals(self):
        state,r=self.base();e=Extraction(updates=[Update(field='application',status='specified',value='高电压储能电池',quote='高电压储能电池',change_relation='refinement')])
        self.send(state,'高电压储能电池',e,r['input_context'])
        self.assertFalse(any(g.get('needs_review') for g in state['fields']['target_performance']))

    def test_real_switch_retains_but_rechecks_related_fields(self):
        state,r=self.base();before=[g['id'] for g in state['fields']['target_performance']]
        e=Extraction(updates=[Update(field='application',status='specified',value='聚合物包装',quote='聚合物包装',change_relation='switch')])
        self.send(state,'改到聚合物包装',e,r['input_context'])
        self.assertEqual([g['id'] for g in state['fields']['target_performance']],before)
        self.assertTrue(all(g.get('needs_review') for g in state['fields']['target_performance']))

    def test_unknown_conditions_and_summaries_require_confirmation(self):
        state=new_state();text='研究陶瓷的导热表现，用于散热'
        e=Extraction(domain='materials_application',domain_quote='用于散热',updates=[Update(field=f,status='specified',value=v,quote=q,**extra) for f,v,q,extra in [('purpose','研究导热表现','研究陶瓷的导热表现',{}),('research_object','陶瓷','陶瓷',{}),('application','散热','用于散热',{}),('target_performance','导热','导热表现',{'direction':'考察'})]])
        r=self.send(state,text,e)
        self.assertEqual(r['intake_status'],'needs_confirmation')
        self.assertEqual(state['fields']['research_scope']['source'],'system_suggestion')
        self.assertEqual(state['fields']['work_conditions']['status'],'unknown')
        r=handle_turn(None,'test',state,'确认',r['input_context']);validate_current_request(r['request'])
        self.assertEqual(r['request']['fields']['research_scope']['source'],'confirmed_suggestion')

    def test_invalid_basis_reference_is_not_applied(self):
        state,r=self.base();before=deepcopy(state['fields'])
        e=Extraction(updates=[Update(field='purpose',status='specified',value='改变目的',quote='改变目的',basis_refs=['missing-id'])])
        r=self.send(state,'改变目的',e,r['input_context'])
        self.assertEqual(state['fields'],before)
        self.assertIn('依据不存在',r['operation_decisions'][0]['reason'])

    def test_recent_context_is_original_visible_output(self):
        state,r=self.base();state['_recent_turns']=[{'input':'用户原话','operation_id':'op','result':{'view':{'text':'实际问题及推荐原文'}}}]
        ctx=build_context(state,'新话');self.assertEqual(ctx['recent_dialogue'][0]['assistant'],'实际问题及推荐原文')
        self.assertEqual(ctx['recent_dialogue'][0]['user'],'用户原话')

    def test_old_view_after_edit_is_rejected(self):
        state,r=self.base();old=r['input_context']
        r=self.send(state,'研究传输',Extraction(updates=[Update(field='purpose',status='specified',value='研究传输',quote='研究传输')]),old)
        self.assertFalse(handle_turn(None,'test',state,'确认',old)['ready_for_design'])

    def test_proposal_derived_context_and_goals_keep_their_source(self):
        state=new_state();r=advice(state);option=state['recommendation_set']['options'][0];ref=option['id']
        text='对这个功能有兴趣'
        e=Extraction(intents=[Intent(id='I',kind='inform',quote=text)],updates=[Update(field='application',intent_ref='I',status='specified',value='所有提案用途',quote=text,basis_refs=[ref]),Update(field='target_performance',intent_ref='I',status='specified',value='热传导',direction='考察',quote=text,basis_refs=[ref])])
        result=self.send(state,text,e,r['input_context'])
        self.assertEqual(state['fields']['application']['status'],'unknown')
        self.assertEqual(state['fields']['target_performance'][0]['source'],'system_suggestion')
        self.assertEqual(state['fields']['target_performance'][0]['recommendation_ref']['direction_id'],ref)
        self.assertTrue(any(h['status']=='focused' for h in state['recommendation_history']))
        self.assertTrue(any(d['decision']=='not_applied' for d in result['operation_decisions']))

    def test_pause_is_acknowledged_without_confirmation(self):
        state,r=self.base()
        with patch('lapis_intake.generate_recommendations') as generated:
            result=self.send(state,'先暂停',Extraction(actions=['explain','pause']),r['input_context'])
            generated.assert_not_called()
        self.assertEqual(result['intake_status'],'paused');self.assertIsNone(result['next_question'])
        self.assertFalse(result['ready_for_design'])

    def test_mixed_adoption_and_rejection_preserves_adopted_direction(self):
        from test_lapis_proposals import proposal
        state=new_state();body=proposal();body['options']=[deepcopy(body['options'][0]) for _ in range(3)]
        for n,o in enumerate(body['options']):o['label']='方向'+str(n+1)
        r=advice(state,body);rec=state['recommendation_set'];ids=[o['id'] for o in rec['options']];text='采用第一个，不用第二第三个'
        e=Extraction(actions=['select','reject'],selected_option=1,intents=[Intent(id='A',kind='adopt',quote='采用第一个',target_refs=[ids[0]]),Intent(id='R',kind='reject',quote='不用第二第三个',target_refs=ids[1:])])
        result=self.send(state,text,e,r['input_context'])
        self.assertEqual({h['direction_id'] for h in state['recommendation_history'] if h['status']=='rejected'},set(ids[1:]))
        self.assertTrue(any(h['status']=='accepted' and h['direction_id']==ids[0] for h in state['recommendation_history']))
        self.assertTrue(any(g.get('recommendation_ref',{}).get('direction_id')==ids[0] for g in state['fields']['target_performance']))
        text='八项已经核对过了，我确认这份规约。第二第三个仍然不用。'
        e=Extraction(actions=['confirm','reject'],intents=[Intent(id='C',kind='confirm',quote='我确认这份规约'),Intent(id='R',kind='reject',quote='第二第三个仍然不用',target_refs=ids[1:])])
        result=self.send(state,text,e,result['input_context'])
        self.assertTrue(result['ready_for_design'])

    def test_full_adoption_duplicate_field_keeps_source_without_false_rejection(self):
        state=new_state();r=advice(state);o=state['recommendation_set']['options'][0];value=o['fields']['research_scope']['value'];text='我采用第一个方向'
        e=Extraction(actions=['select'],selected_option=1,intents=[Intent(id='A',kind='adopt',quote=text,target_refs=[o['id']])],updates=[Update(field='research_scope',status='specified',value=value,quote=text,intent_ref='A',basis_refs=[o['id']])])
        result=self.send(state,text,e,r['input_context'])
        self.assertEqual(state['fields']['research_scope']['source'],'confirmed_suggestion')
        self.assertFalse(any(d['decision']=='not_applied' for d in result['operation_decisions']))
        self.assertTrue(any(d['decision']=='covered_by_adoption' for d in result['operation_decisions']))

    def test_unknown_detail_update_cannot_create_stuck_goal_issue(self):
        state,r=self.base();before=deepcopy(state['fields']['target_performance']);text='详细指标还不知道'
        e=Extraction(intents=[Intent(id='I',kind='inform',quote=text)],updates=[Update(field='target_performance',status='unknown',quote=text,action='update',intent_ref='I')])
        result=self.send(state,text,e,r['input_context'])
        self.assertEqual(state['fields']['target_performance'],before)
        self.assertFalse(result['blocking_issues'])
        self.assertEqual(result['operation_decisions'][0]['decision'],'not_applied')

    def test_coarse_purpose_can_be_reviewed_without_choosing_a_paradigm(self):
        state=new_state();text='电解液用于储能电池，关注传输，条件还不知道'
        e=Extraction(domain='materials_application',domain_quote='用于储能电池',updates=[Update(field='research_object',status='specified',value='电解液',quote='电解液'),Update(field='application',status='specified',value='储能电池',quote='用于储能电池'),Update(field='target_performance',status='specified',value='传输',direction='考察',quote='关注传输'),Update(field='work_conditions',status='unknown',quote='条件还不知道')])
        r=self.send(state,text,e)
        self.assertEqual(r['intake_status'],'needs_confirmation')
        self.assertEqual(state['fields']['purpose']['source'],'system_suggestion')
        self.assertFalse(r['ready_for_design'])
        result=handle_turn(None,'test',state,'确认',r['input_context'])
        self.assertTrue(result['ready_for_design'])
        self.assertEqual(result['request']['fields']['purpose']['source'],'confirmed_suggestion')

    def test_explicit_confirmation_with_unchanged_restatement_is_accepted(self):
        state,r=self.base();value=state['fields']['purpose']['value'];text='确认，采用这版规约；已有目标和约束不变。'
        e=Extraction(actions=['confirm','inform','progress'],intents=[Intent(id='I',kind='confirm',quote=text)],updates=[Update(field='purpose',status='specified',value=value,quote=text,change_relation='restatement')])
        result=self.send(state,text,e,r['input_context'])
        self.assertTrue(result['ready_for_design']);self.assertTrue(result['confirmation_event'])

    def test_restatement_of_unknown_method_cannot_clear_known_purpose(self):
        state,r=self.base();before=deepcopy(state['fields']['purpose']);text='筛选还是探索还没定，先按这个方向走'
        e=Extraction(intents=[Intent(id='I',kind='inform',quote=text)],updates=[Update(field='purpose',status='open',quote=text,intent_ref='I',change_relation='restatement')])
        result=self.send(state,text,e,r['input_context'])
        self.assertEqual(state['fields']['purpose'],before)
        self.assertEqual(result['operation_decisions'][0]['decision'],'not_applied')

    def test_named_absent_removal_does_not_create_permanent_issue(self):
        state,r=self.base();before=deepcopy(state['fields']['target_performance']);text='强度不用研究了'
        e=Extraction(intents=[Intent(id='I',kind='edit',quote=text,target_refs=['强度'])],updates=[Update(field='target_performance',status='none',value='强度',quote=text,intent_ref='I',action='remove',target_value='强度')])
        result=self.send(state,text,e,r['input_context'])
        self.assertEqual(state['fields']['target_performance'],before)
        self.assertFalse(result['blocking_issues'])

    def test_model_confirm_misclassification_cannot_approve_progress(self):
        state,r=self.base()
        e=Extraction(actions=['confirm'],intents=[Intent(id='I',kind='confirm',quote='可以了，进行下一步吧')])
        result=self.send(state,'可以了，进行下一步吧',e,r['input_context'])
        self.assertFalse(result['ready_for_design']);self.assertFalse(result.get('confirmation_event'))
        self.assertEqual(result['intake_status'],'needs_confirmation')

    def test_v4_is_readable_but_not_current_permission(self):
        state,r=self.base();old=deepcopy(r['request']);old.update(contract_version=4,rule_version='intake-rules-4.0')
        frozen=deepcopy(old);validate_research_request_v4(old)
        with self.assertRaises(ValueError):validate_current_request(old)
        legacy={'contract_version':4,'fields':deepcopy(old['fields']),'turns':[TEXT],'stage':'ready_for_design','proposals':{'old-proof':{'version':1}}}
        result=handle_turn(None,'test',legacy,'确认',{'draft_id':old['draft_id']})
        self.assertFalse(result['ready_for_design']);self.assertEqual(old,frozen)
        self.assertIn('old-proof',legacy['proposals'])

    def test_quote_intent_cannot_create_user_reference(self):
        state,r=self.base();e=Extraction(intents=[Intent(id='I',kind='quote',quote='假设尚未核验')],updates=[Update(field='reference_note',intent_ref='I',status='specified',value='假设尚未核验',quote='假设尚未核验')])
        self.send(state,'假设尚未核验',e,r['input_context']);self.assertFalse(state['reference_notes'])

if __name__=='__main__':unittest.main()
