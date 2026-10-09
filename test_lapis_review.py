import io
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from unittest.mock import Mock, patch
from lapis import render_intake_result, render_sources, run_chat
from lapis_intake import Extraction, Update, handle_turn, new_state
from lapis_proposals import Guidance
from test_lapis_proposals import proposal


def advice(state, body=None, text='请推荐', actions=None, context=None):
    client = Mock(); client.create.return_value = Guidance.model_validate(body or proposal())
    with patch('lapis_intake.extract',return_value=Extraction(actions=actions or ['recommend'],selected_option=1)), patch('lapis_proposals.reference_recommendations',return_value=None):
        return handle_turn(client,'review-test',state,text,context)


def select(state, context):
    with patch('lapis_intake.extract',return_value=Extraction(actions=['select'],selected_option=1)):
        return handle_turn(None,'review-test',state,'采用第一个方向',context)


def shown(result, full=False):
    output=io.StringIO()
    with redirect_stdout(output): render_intake_result(result,full=full)
    return output.getvalue()


class ReviewTest(unittest.TestCase):
    def test_adopted_hard_constraint_survives_later_recommendation(self):
        state=new_state(); body=proposal()
        body['options'][0]['fields']['constraints']=[{'status':'specified','value':'不得使用含氟添加剂','strength':'hard'}]
        r=advice(state,body); first=select(state,r['input_context'])
        r=advice(state,context=first['input_context']); later=select(state,r['input_context'])
        entries=later['request']['fields']['constraints']
        self.assertTrue(any(e.get('value')=='不得使用含氟添加剂' and e.get('strength')=='hard' for e in entries))
        self.assertFalse(any(e['status']=='unknown' for e in entries))

    def test_economic_withdrawal_uses_model_resolved_name(self):
        state=new_state(); state['fields']['constraints']=[{'id':'cost','status':'unclear','value':'性价比高','source':'user','quote':'性价比高','turn':1,'strength':'preference'}]
        body=proposal();body['options'][0]['fields']['constraints']=[{'status':'specified','value':'性价比高','strength':'preference'}]
        r=advice(state,body)
        extraction=Extraction(actions=['select','edit'],selected_option=1,updates=[Update(field='constraints',status='none',quote='先不考虑成本',action='remove',target_value='性价比高')])
        with patch('lapis_intake.extract',return_value=extraction):
            result=handle_turn(None,'test',state,'采用第一个，但先不考虑成本',r['input_context'])
        self.assertFalse(any(e.get('value')=='性价比高' for e in state['fields']['constraints']))
        self.assertFalse(any('无法唯一匹配' in i['message'] for i in result['blocking_issues']))

    def test_identical_rejected_content_filtered_and_explicit_reconsideration_allowed(self):
        state=new_state(); first=advice(state)
        later=advice(state,text='拒绝第一个，换方向',actions=['reject','recommend'],context=first['input_context'])
        self.assertEqual(later['recommendations']['options'],[])
        self.assertNotIn('采用序号',later['next_question'])
        revived=advice(state,text='我重新考虑之前拒绝的方向')
        self.assertTrue(revived['recommendations']['options'][0]['reconsidered'])
        adopted=select(state,revived['input_context'])
        self.assertEqual(adopted['intake_status'],'needs_confirmation')

    def test_review_and_fresh_copy_preserve_adopted_assumptions(self):
        state=new_state();body=proposal()
        body['options'][0]['assumptions']=['ASSUMPTION_MARKER']
        body['options'][0]['limitations']=['LIMITATION_MARKER']
        r=advice(state,body); review=select(state,r['input_context'])
        text=shown(deepcopy(review))
        self.assertIn('ASSUMPTION_MARKER',text); self.assertIn('LIMITATION_MARKER',text)
        self.assertEqual(sum(label+'：' in text for label in __import__('lapis_intake').LABELS.values()),8)
        self.assertNotIn('recommendation_ref',text);self.assertNotIn('draft_id',text)
        confirmed=handle_turn(None,'test',state,'确认',review['input_context'])
        self.assertIn('LIMITATION_MARKER',shown(confirmed,full=True))

    def test_collection_and_edit_do_not_print_internal_unknown_fields(self):
        state=new_state()
        with patch('lapis_intake.extract',return_value=Extraction(updates=[Update(field='research_object',status='specified',value='呋喃基',quote='呋喃基')])):
            result=handle_turn(None,'test',state,'我想知道呋喃基有什么特性')
        text=shown(result)
        self.assertIn('呋喃基',text)
        for token in ('unknown','source','draft_id','D1-','工作条件：','约束条件：','"value"'):
            self.assertNotIn(token,text)
        self.assertIn('工作条件：',shown(result,full=True))

    def test_empty_explanation_has_no_nonexistent_choice_or_confirmation(self):
        state=new_state(); empty=advice(state,{'explanation':'请先限定材料用途。','options':[]})
        self.assertNotIn('序号',shown(empty));self.assertNotIn('可选方向',shown(empty))
        self.assertFalse(handle_turn(None,'test',state,'确认',empty['input_context'])['ready_for_design'])

    def test_guidance_hides_complete_review_so_first_confirm_must_display_it(self):
        state=new_state(); initial=advice(state); review=select(state,initial['input_context'])
        guidance=advice(state,context=review['input_context'])
        self.assertNotIn('当前研究规约',shown(guidance))
        pending=handle_turn(None,'test',state,'确认',guidance['input_context'])
        self.assertFalse(pending['ready_for_design'])
        self.assertEqual(pending['intake_status'],'needs_confirmation')
        self.assertIn('当前研究规约',shown(pending))
        self.assertTrue(handle_turn(None,'test',state,'确认',pending['input_context'])['ready_for_design'])

    def test_explicit_field_reaffirmation_promotes_bounded_open_intent_without_erasing_it(self):
        state=new_state(); body=proposal()
        body['options'][0]['fields']['material_function']['status']='open'
        initial=advice(state,body); selected=select(state,initial['input_context'])
        with patch('lapis_intake.extract',return_value=Extraction(actions=['reaffirm'],reaffirm_fields=['material_function'],updates=[
                Update(field='material_function',status='open',quote='功能沿用刚才提出的传导热量')])):
            result=handle_turn(None,'test',state,'功能沿用刚才提出的传导热量',selected['input_context'])
        self.assertEqual(result['intake_status'],'needs_confirmation')
        self.assertEqual(state['fields']['material_function']['value'],'拟传导热量')
        self.assertEqual(state['fields']['material_function']['source'],'confirmed_suggestion')

    def test_old_summary_loads_checked_immutable_operation(self):
        from lapis import hydrate_review
        state=new_state(); initial=advice(state); review=select(state,initial['input_context'])
        old=deepcopy(review)
        for summary in old['request']['proposals'].values():
            summary.pop('review')
            summary['generation']['operation_id']='old-operation'
        with patch('lapis.get_intake_operation',return_value={'result':initial}):
            loaded=hydrate_review(old)
        self.assertIn('未核验材料性能',shown(loaded))
        self.assertNotIn('review',next(iter(old['request']['proposals'].values())))
        with patch('lapis.get_intake_operation',return_value={'result':{}}):
            with self.assertRaises(ValueError): hydrate_review(old)

    def test_incomplete_selection_does_not_summarize_unknown_placeholders(self):
        state=new_state();body=proposal()
        body['options'][0]['fields']['purpose']={'status':'open'}
        body['options'][0]['fields']['application']={'status':'open'}
        initial=advice(state,body); selected=select(state,initial['input_context'])
        text=shown(selected)
        self.assertNotIn('尚未指定',text)
        self.assertNotIn('研究目的为留待',text)
        self.assertFalse(selected['ready_for_design'])

    def test_user_method_references_are_readable_without_exposing_json(self):
        stream=io.StringIO()
        result={'request':{'reference_notes':[{'value':'用户指定的方法需审核','active':True}, {'value':'撤回参考','active':False}]}}
        with redirect_stdout(stream): render_sources(result)
        self.assertIn('用户指定的方法需审核',stream.getvalue())
        self.assertNotIn('撤回参考',stream.getvalue())
        self.assertNotIn('active',stream.getvalue())

    def test_show_receipt_can_confirm_current_complete_guidance_but_not_old_version(self):
        state=new_state(); initial=advice(state); selected=select(state,initial['input_context'])
        guidance=advice(state,context=selected['input_context'])
        context={**guidance['input_context'],'reviewed_draft_id':'old'}
        before=deepcopy(state)
        self.assertFalse(handle_turn(None,'test',state,'确认',context)['ready_for_design'])
        state=before
        self.assertTrue(handle_turn(None,'test',state,'确认',{**guidance['input_context'],'reviewed_draft_id':state['draft_id']})['ready_for_design'])

    def test_chat_show_carries_display_receipt_without_changing_default_retry_context(self):
        state=new_state(); initial=advice(state); selected=select(state,initial['input_context'])
        guidance=advice(state,context=selected['input_context'])
        with patch.dict('os.environ',{'LAPIS_API_KEY':'test'}),patch('lapis.get_task',return_value={'intake_result':guidance}),patch('lapis.OpenAI'),patch('lapis.instructor.from_openai'),patch('lapis.run_intake_turn',return_value={'result':{'ready_for_design':True,'intake_status':'ready_for_design'}}) as turn,patch('builtins.input',side_effect=['/show','确认','/exit']),redirect_stdout(io.StringIO()):
            run_chat('test-task','test')
        self.assertEqual(turn.call_args.kwargs['input_context']['reviewed_draft_id'],guidance['request']['draft_id'])

    def test_show_sources_debug_commands_do_not_call_model_or_repeat_review(self):
        state=new_state(); initial=advice(state); review=select(state,initial['input_context'])
        stream=io.StringIO()
        with patch.dict('os.environ',{'LAPIS_API_KEY':'test'}),patch('lapis.get_task',return_value={'intake_result':review}),patch('lapis.OpenAI'),patch('lapis.instructor.from_openai'),patch('lapis.run_intake_turn') as turn,patch('builtins.input',side_effect=['/show','/sources','/exit']),redirect_stdout(stream):
            run_chat('test-task','test')
        turn.assert_not_called()
        self.assertEqual(stream.getvalue().count('当前研究规约：'),2)

if __name__=='__main__': unittest.main()
