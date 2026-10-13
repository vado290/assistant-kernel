"""Only agreed subjective criteria add a version-bound existing QuestionGroup."""
import hashlib

from psycopg.types.json import Jsonb

from assistant_kernel.services.prompt_protocol import PromptProtocol
from assistant_kernel.services.question_groups import QuestionGroupService


class AcceptanceFeedbackV12:
    def __init__(self,settings,repository):
        self.settings=settings;self.repository=repository;self.prompts=PromptProtocol(settings,repository)

    async def ensure_locked(self,connection,*,task,refs,docs,source):
        criteria=docs['task_contract']['payload']['acceptance_criteria']
        required=[item for item in criteria if item['required']]
        missing=[item['criterion_id'] for item in required if 'requires_user_feedback' not in item]
        if missing:
            raise PermissionError('Historical acceptance criteria lack an explicit feedback classification; retain original standards and approve only affected clarification: '+','.join(missing))
        subjective=[item for item in required if item['requires_user_feedback']]
        if not subjective:return {'status':'not_required','feedback_refs':[]}
        question_ids={item['criterion_id']:'feedback-'+hashlib.sha256(item['criterion_id'].encode()).hexdigest()[:16] for item in subjective}
        request=task['request_json'] or {};accepted=request.get('accepted_documents') or {}
        publication=accepted.get('publication')
        if publication:versions=self.prompts.documents.read(task['project_id'],publication)['payload']['actual_version_refs']
        elif docs.get('implementation_report'):versions=docs['implementation_report']['payload']['actual_changes']
        else:versions=docs['review_report']['payload']['deliverable_refs']
        if not versions:raise PermissionError('Subjective feedback needs the actual delivery versions')
        basis=self.prompts.write_blob(task['project_id'],str(task['run_id']),'acceptance-feedback-basis',{
            'task_contract_ref':refs['task_contract'],'delivery_version_refs':versions,
            'criterion_ids':[item['criterion_id'] for item in subjective]})
        row=await (await connection.execute("""SELECT * FROM assistant.messages WHERE project_id=%s AND run_id=%s
            AND message_type='question_group' AND content_json->>'acceptance_basis_sha256'=%s
            ORDER BY created_at DESC LIMIT 1 FOR UPDATE""",(task['project_id'],task['run_id'],basis['sha256']))).fetchone()
        groups=QuestionGroupService(self.settings,self.repository)
        if not row:
            questions=[{'question_id':question_ids[item['criterion_id']],'question_text':
                '请按标准 '+item['criterion_id']+' 评价这份具体交付：'+item['expected_result']+'\n交付：'+
                    '\n'.join(ref['uri']+'（版本 '+ref['sha256'][:12]+'）' for ref in versions),
                'topic':'final_acceptance','required':True,'options':[{'id':'accept','label':'满足','description':'该条达到约定标准'},
                    {'id':'revise','label':'需修改','description':'该条仍需改进'}],
                'recommended_option_id':None,'recommendation_reason':'请依据实际交付评价。','source_refs':[refs['task_contract'],basis]+versions}
                for item in subjective]
            routes=await groups.resolve_routes_locked(connection,owner=task,source=source,interaction_scope='pm_business')
            row=await groups.create_locked(connection,owner=task,questions=questions,source_message_id=str(source['message_id']),routes=routes)
            body={**row['content_json'],'acceptance_basis_sha256':basis['sha256'],'acceptance_basis_ref':basis}
            await connection.execute('UPDATE assistant.messages SET content_json=%s WHERE message_id=%s',(Jsonb(body),row['message_id']))
            row={**row,'content_json':body}
        group=row['content_json']['group'];answers={item['question_id']:item for item in group['accepted_answers']}
        owner=await (await connection.execute('SELECT user_id FROM assistant.messages WHERE message_id=%s',(task['trigger_message_id'],))).fetchone()
        receipts=[];unmet=[];rejected=[]
        for item in subjective:
            answer=answers.get(question_ids[item['criterion_id']])
            if not answer:unmet.append(item['criterion_id']);continue
            event=await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',(answer['source_message_ref']['id'],))).fetchone()
            body=(event or {}).get('content_json') or {}
            if (not event or event['project_id']!=task['project_id'] or event['direction']!='inbound'
                    or event['user_id']!=owner['user_id'] or body.get('formal_answers_accepted') is not True
                    or (body.get('question_group_ref') or {}).get('id')!=str(row['message_id'])
                    or answer['content_version']!=group['content_version']):
                raise PermissionError('Final feedback has no actual matching criterion/version-bound human source')
            value=answer['value']
            positive=value is True or isinstance(value,str) and value.strip() in {'满足','accept','通过','是'}
            if not positive:
                rejected.append({'criterion_id':item['criterion_id'],'source_ref':answer['source_message_ref']});continue
            receipts.append(answer['source_message_ref'])
        if rejected:
            await connection.execute("UPDATE assistant.runs SET status='waiting_supervisor',request_json=request_json || %s WHERE run_id=%s",
                (Jsonb({'rework_class':'implementation','acceptance_feedback_rejections':rejected}),task['run_id']))
            return {'status':'rework_required','basis_ref':basis,'rejected':rejected}
        if unmet:
            await connection.execute("UPDATE assistant.runs SET status='waiting_user',waiting_question_message_id=%s,request_json=request_json || %s WHERE run_id=%s",
                (row['content_json']['projection_ids'][0],Jsonb({'pending_acceptance_feedback_ref':basis,'pending_acceptance_criterion_ids':unmet}),task['run_id']))
            return {'status':'waiting_user_feedback','criterion_ids':unmet,'basis_ref':basis,'group_id':str(row['message_id'])}
        return {'status':'accepted','basis_ref':basis,'feedback_refs':receipts}
