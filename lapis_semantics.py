"""Intake context and common operation boundaries; no material vocabulary rules."""
from copy import deepcopy
import json
from functools import lru_cache
import hashlib
import importlib.metadata
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4
from lapis_contract import FIELDS, LIST_FIELDS, content_hash

RUN_ID = str(uuid4())

@lru_cache(maxsize=1)
def runtime_fingerprint():
    root=Path(__file__).parent
    result={'run_id':RUN_ID,'pid':os.getpid(),'code_hashes':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('lapis*.py')},'packages':{}}
    for name in ('instructor','pydantic','langgraph','psycopg'):
        try:result['packages'][name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:result['packages'][name]='unavailable'
    try:
        result['commit']=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
        result['dirty']=bool(subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True).strip())
    except (OSError,subprocess.CalledProcessError):result['commit']='unavailable'
    return result


def build_context(state,text):
    recent=[]
    for row in state.get('_recent_turns',[])[-4:]:
        result=row.get('result') or {}
        view=result.get('view') or {}
        recent.append({'operation_id':row.get('operation_id'),'user':row.get('input'),'assistant':view.get('text') or result.get('next_question'),'original_view':bool(view)})
    context = {'input':text,'original_intent':state['turns'][0] if state['turns'] else text,'fields':state['fields'],
            'issues':[i for i in state['issues'] if i.get('status')!='resolved'],'current_question':state.get('current_question'),
            'asked_field':state.get('asked_field'),'recent_dialogue':recent,'recommendations':state.get('recommendation_set'),
            'adopted_proposals':state.get('proposals',{}),'recommendation_history':state.get('recommendation_history',[])[-4:],
            'reference_notes':state.get('reference_notes',[])}
    while len(json.dumps(context,ensure_ascii=False))>24000 and context['recent_dialogue']:
        context['recent_dialogue'].pop(0);context['older_dialogue_omitted']=True
    if len(json.dumps(context,ensure_ascii=False))>24000:
        raise ValueError('必要研究上下文超过本轮资源限制，请先缩小输入或查看草稿')
    return context


def proposal_echo(state,text):
    """Exact known displayed content is a citation, never a new user requirement."""
    norm=lambda s:re.sub(r'[\s；;，,。:：]+','',s)
    known=[]
    for option in (state.get('recommendation_set') or {}).get('options',[]):
        for key in ('label','reason','assumptions','limitations','clarifications'):
            v=option.get(key,[]);known.append(norm(v if isinstance(v,str) else '；'.join(v)))
        value=(option.get('fields',{}).get('purpose') or {})
        known.append(norm(value.get('value','') if isinstance(value,dict) else value))
    for proposal in state.get('proposals',{}).values():
        for item in proposal.get('review',{}).values():
            for key in ('label','reason','assumptions','limitations','clarifications'):
                v=item.get(key,[]);known.append(norm(v if isinstance(v,str) else '；'.join(v)))
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    if not lines:return False
    for line in lines:
        value=re.sub(r'^(?:拟研究|假设|限制|待澄清|待研究设计核查)\s*[:：]\s*','',line)
        value=norm(value)
        labeled=bool(re.match(r'^(?:拟研究|假设|限制|待澄清|待研究设计核查)\s*[:：]',line))
        if (len(value)<8 and not labeled) or not any(value==k or value in k for k in known if k):return False
    return True


def check_operations(state,text,extraction):
    """Check referential/operation scope; semantic ambiguity remains a user question."""
    intents={i.id:i for i in extraction.intents}
    question=state.get('current_question') or {}
    refs=set(FIELDS)|{e.get('id') for v in state['fields'].values() for e in (v if isinstance(v,list) else [v]) if e.get('id')}
    rec=state.get('recommendation_set') or {}
    refs.update(o['id'] for o in rec.get('options',[]));refs.update(state.get('proposals',{}));refs.update(p for proposal in state.get('proposals',{}).values() for p in proposal.get('direction_ids',[]))
    if rec.get('id'):refs.add(rec['id'])
    accepted=[];decisions=[]
    for item in extraction.updates:
        if item.field == 'research_scope' and item.scope_mode in {'current_scope','no_extra_exclusions'}:
            base=state['fields']['research_scope'].get('value')
            if not base:
                obj=state['fields']['research_object'].get('value');app=state['fields']['application'].get('value')
                goals='、'.join(g.get('value') or '' for g in state['fields']['target_performance'] if g.get('status')=='specified')
                base=(str(obj)+'在'+str(app)+'中的'+goals+'研究') if obj and app and goals else None
            if base:
                suffix='；本轮无额外排除项' if item.scope_mode=='no_extra_exclusions' and '本轮无额外排除项' not in base else ''
                item=item.model_copy(update={'value':base+suffix,'status':'specified','change_relation':'refinement','basis_refs':['research_object','application','research_scope']})
        reason=None;intent=intents.get(item.intent_ref)
        from_proposal=any(ref in {o['id'] for o in rec.get('options',[])} for ref in item.basis_refs)
        if from_proposal and item.field in {'research_object','application','research_scope','work_conditions','constraints'} and item.value and item.value not in item.quote and not (intent and intent.kind=='edit'):
            reason='这项来自提案，尚未见到你对该用途、条件或范围的明确选择'
        if item.quote not in text:reason='原话片段不属于本轮'
        elif item.intent_ref and not intent:reason='意图引用不存在'
        elif any(ref not in refs for ref in item.basis_refs):reason='上下文依据不存在'
        elif intent and intent.quote not in text:reason='意图依据不属于本轮'
        elif intent and intent.kind=='quote':reason='引用不是新增用户要求'
        elif intent and intent.kind=='answer' and (intent.question_ref!=question.get('id') or item.field not in question.get('fields',[])):
            labels=dict(zip(FIELDS,('研究目的','研究对象','应用场景','工作条件','目标性能','约束条件','研究范围','材料功能')))
            explicit=item.field in intent.target_refs and intent.quote.startswith(labels.get(item.field,'\0'))
            if not explicit:reason='回答操作不属于当前问题；跨字段编辑须有独立意图'
        if item.field in FIELDS and item.field not in LIST_FIELDS:
            existing=state['fields'][item.field]
            if existing.get('status')=='specified' and item.status!='specified' and item.change_relation=='restatement':
                reason='重述没有撤回既有研究内容；保持当前值'
        if item.field in LIST_FIELDS:
            current=state['fields'][item.field]
            existing=[e for e in current if e.get('status')=='specified']
            name=item.target_value or item.value
            if item.action=='remove' and intent and intent.kind=='edit' and name and name in item.quote and not any(name in (e.get('value') or '') or (e.get('value') and e['value'] in name) for e in current) and not any(e.get('id')==item.target_id if item.target_id else e.get('value')==name for e in current):
                reason='当前列表没有该条目，未删除其他项'
            destructive=item.action in {'remove','replace'} or (item.action=='update' and any((e.get('id')==item.target_id or e.get('value')==item.target_value) and (item.status!='specified' or e.get('value')!=item.value) for e in existing))
            if destructive and existing:
                if intent and intent.kind != 'edit':reason='该意图没有授权撤回或替换既有条目'
                elif not intent and item.action=='replace' and not re.search(r'目标.*(?:只|仅|改为|替换)|只(?:比较|保留)|没有.*约束|不设置.*约束',item.quote):
                    reason='整体替换缺少明确修改依据'
                if intent and item.action=='replace' and item.field not in intent.target_refs and not {e['id'] for e in existing}.issubset(set(intent.target_refs)):
                    reason='整体替换没有明确指向当前完整字段'
                if intent and item.action=='remove' and not (item.target_id in intent.target_refs or item.target_value in intent.target_refs):
                    reason='撤回没有对应的目标引用'
        if reason:
            decisions.append({'operation':item.model_dump(),'decision':'not_applied','reason':reason})
        else:
            accepted.append(item);decisions.append({'operation':item.model_dump(),'decision':'accepted'})
    return extraction.model_copy(update={'updates':accepted}),decisions


def prepare_summaries(state,operation_id):
    """Only restate the known object, application and goals; adoption is deferred."""
    f=state['fields'];obj=f['research_object'];app=f['application'];goals=f['target_performance']
    if obj.get('status')!='specified' or app.get('status')!='specified':return
    values=[g['value'] for g in goals if g.get('status')=='specified' and g.get('value')]
    if not values:return
    focus='、'.join(values)
    summaries={'purpose':'研究'+obj['value']+'在'+app['value']+'中的'+focus+'表现',
               'research_scope':obj['value']+'在'+app['value']+'中的'+focus+'研究；具体候选、条件与方法留待研究设计',
               'material_function':'拟在'+app['value']+'中支持'+focus+'；效果尚待研究验证'}
    for field,value in summaries.items():
        current=f[field]
        if field=='purpose' and current.get('value'):continue
        if current.get('source')=='system_suggestion' and current.get('suggestion_origin')=='context' and current.get('value')!=value:
            current={'status':'unknown'}
        if current.get('status') not in {'unknown','open'}:continue
        # Preserve a known proposed scope rather than generating a broader one.
        if current.get('value') and current.get('recommendation_ref'):
            f[field]={**current,'status':'specified','source':'system_suggestion'};continue
        rid='summary-'+str(uuid4());did=rid+'-1'
        state['proposals'][rid]={'version':1,'draft_id':state['draft_id'],'generation':{'origin':'context','generator_version':'bounded-summary-1','operation_id':operation_id},
            'direction_ids':[did],'content_sha256':content_hash(value),'review':{did:{'label':'已有意图摘要','assumptions':[],'limitations':['摘要只整理研究意图，不证明材料功能；未定条件和方法需研究设计核查'],'clarifications':[],'source_refs':[]}}}
        f[field]={'status':'specified','value':value,'source':'system_suggestion','quote':None,'turn':None,
            'suggestion_origin':'context','evidence_status':'unverified','basis_refs':['research_object','application']+[g['id'] for g in goals if g.get('status')=='specified'],
            'recommendation_ref':{'id':rid,'version':1,'direction_id':did},'source_refs':[]}


def adopt_summaries(state,text):
    for value in state['fields'].values():
        for entry in value if isinstance(value,list) else [value]:
            if entry.get('source')=='system_suggestion':
                entry.update(source='confirmed_suggestion',selection_quote=text,confirmed_turn=len(state['turns']))
