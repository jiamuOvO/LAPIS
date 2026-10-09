"""Short contextual replies and substantive confirmation gates."""
import unittest
from unittest.mock import patch
from lapis_intake import Extraction, Update, Issue, handle_turn, new_state
from test_lapis_review import advice, select, shown

class ShortReplyTest(unittest.TestCase):
    def test_partial_reply_does_not_adopt_whole_option(self):
        state=new_state(); r=advice(state)
        extraction=Extraction(actions=['select'],selected_option=1,selection_mode='partial',updates=[
            Update(field='target_performance',status='specified',value='抗氧化作用',direction='抗氧化作用',quote='抗氧化作用吧',action='replace'),
            Update(field='purpose',status='specified',value='探索抗氧化材料应用',quote='抗氧化作用吧'),
            Update(field='application',status='specified',value='包装材料',quote='抗氧化作用吧')])
        with patch('lapis_intake.extract',return_value=extraction):
            result=handle_turn(None,'test',state,'抗氧化作用吧',r['input_context'])
        self.assertEqual(state['fields']['application']['status'],'unknown')
        self.assertEqual(len(state['fields']['target_performance']),1)
        self.assertFalse(state['proposals'])
        self.assertNotIn('抗氧化作用（抗氧化作用）',shown(result))

    def test_ambiguous_multiple_matches_asks_specific_difference(self):
        state=new_state();r=advice(state)
        state['domain']='materials_application'
        extraction=Extraction(actions=['inform'],issues=[Issue(field='application',message='用途有多个选择',quote='保护作用吧')],clarification_field='application',clarification_question='您想用于包装材料还是表面涂层？')
        with patch('lapis_intake.extract',return_value=extraction):
            result=handle_turn(None,'test',state,'保护作用吧',r['input_context'])
        self.assertFalse(state['proposals'])
        self.assertIn('包装材料还是表面涂层',result['next_question'])
        with patch('lapis_intake.extract',return_value=Extraction(actions=['progress'])):
            progress=handle_turn(None,'test',state,'可以下一步了吗',result['input_context'])
        self.assertIn('包装材料还是表面涂层',progress['next_question'])

    def test_actual_gap_precedes_display_gate(self):
        state=new_state();state['domain']='materials_application';state['fields']['purpose'].update(status='unclear',value='研究生活用途',source='user')
        result=handle_turn(None,'test',state,'确认',{})
        self.assertIn('研究目的',result['next_question'])
        self.assertIn('研究生活用途',result['next_question'])
        self.assertNotIn('这项信息',shown(result))
        self.assertNotIn('请先查看',result.get('notice',''))

    def test_progress_displays_full_review_without_confirming(self):
        state=new_state();r=advice(state);r=select(state,r['input_context'])
        with patch('lapis_intake.extract',return_value=Extraction(actions=['progress'])):
            result=handle_turn(None,'test',state,'可以下一步了吗',r['input_context'])
        self.assertEqual(result['intake_status'],'needs_confirmation')
        self.assertIn('当前研究规约',shown(result))
        self.assertFalse(result.get('confirmation_event'))

    def test_scalar_clarification_supersedes_old_ambiguity_not_conflict(self):
        state=new_state();state["fields"]["purpose"].update(status="unclear",value="生活用途",source="user")
        state["issues"]=[{"id":"old","field":"purpose","kind":"ambiguity","status":"open","message":"旧目的过宽"},{"id":"conflict","field":"purpose","kind":"conflict","status":"open","message":"独立矛盾"}]
        with patch("lapis_intake.extract",return_value=Extraction(updates=[Update(field="purpose",status="specified",value="探索抗氧化材料应用",quote="抗氧化作用吧")])):
            handle_turn(None,"test",state,"抗氧化作用吧",{})
        self.assertEqual(state["issues"][0]["status"],"resolved")
        self.assertEqual(state["issues"][1]["status"],"open")

    def test_excluding_drug_screening_is_not_drug_discovery(self):
        from lapis_contract import domain_from_fields
        fields={'purpose':{'value':'探索涂层抗氧化'},'application':{'value':'包装涂层'},'research_scope':{'value':'不做药物筛选'}}
        self.assertEqual(domain_from_fields(fields),'materials_application')
        fields['research_scope']['value']='药物先导筛选'
        self.assertEqual(domain_from_fields(fields),'drug_discovery')

    def test_old_display_after_edit_cannot_confirm(self):
        state=new_state();r=advice(state);r=select(state,r['input_context']);old=r['input_context']
        with patch('lapis_intake.extract',return_value=Extraction(updates=[Update(field='purpose',status='specified',value='比较材料',quote='比较材料')])):
            handle_turn(None,'test',state,'比较材料',old)
        result=handle_turn(None,'test',state,'确认',old)
        self.assertFalse(result.get('confirmation_event'))
        self.assertIn('当前研究规约',shown(result))

if __name__=='__main__':unittest.main()
