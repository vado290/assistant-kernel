"""Deterministic directory disclosures attached to existing question messages."""
import hashlib
from pathlib import Path
import re

import yaml
from assistant_kernel.services.document_runtime import resource_ref
from assistant_kernel.storage import ProjectRuntimeManager


def enrich_questions(settings,project_id,run_id,questions):
    runtime=ProjectRuntimeManager(settings.data_root)
    root=runtime.describe(project_id).vault_root
    directory=runtime.resolve_below(root,'runs/'+run_id)
    subjects={};updated=[]
    for question in questions:
        item=dict(question);identity=item.get('question_id','');path=None
        if identity.startswith('project-create:'):
            target=identity.partition(':')[2]
            if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}',target):path=directory/('project-proposal-'+target+'.yaml')
        elif identity.startswith('source-scope:'):
            fragment=identity.rsplit(':',1)[-1]
            if re.fullmatch(r'[0-9a-f]{16}',fragment):path=directory/('source-scope-proposal-'+fragment+'.yaml')
        if path is not None and path.is_file():
            encoded=path.read_bytes();proposal=yaml.safe_load(encoded)
            if proposal.get('approval_question_id')!=identity or proposal.get('source_run_id')!=run_id:
                raise ValueError('Directory disclosure belongs to another proposal')
            if proposal.get('presentation_version')=='directory-disclosure.v1':
                lines=[str(item['question_text']),
                    '项目：'+str(proposal.get('project_id') or proposal.get('target_project_id')),
                    '目的：'+str(proposal.get('purpose') or proposal.get('reason') or ''),
                    str(proposal['directory_disclosure'])]
                if proposal.get('boundaries'):lines.append('范围约束：'+'；'.join(proposal['boundaries']))
                if proposal.get('non_goals'):lines.append('不包含：'+'；'.join(proposal['non_goals']))
                if proposal.get('project_id'):
                    lines.append('项目自有目录：'+str(runtime.describe(proposal['project_id']).vault_root))
                item['question_text']='\n'.join(lines)
                subjects[identity]=resource_ref(kind='source',identity=identity,
                    uri='vault://'+project_id+'/'+path.relative_to(root).as_posix(),
                    sha256=hashlib.sha256(encoded).hexdigest(),purpose='fixed proposal shown in this exact question')
        updated.append(item)
    return updated,subjects


async def require_disclosed_subject(connection,*,run_id,answer_message_id,question_id,proposal_uri,proposal_sha256):
    if not answer_message_id:raise PermissionError('A real answer to the displayed proposal is required')
    row=await (await connection.execute("""SELECT q.content_json,a.user_id,a.project_id
        FROM assistant.messages a JOIN assistant.messages q ON q.message_id=a.reply_to_message_id
        WHERE a.message_id=%s AND a.direction='inbound' AND q.direction='outbound'
          AND q.run_id=%s AND a.project_id=q.project_id
          AND a.user_id IS NOT DISTINCT FROM q.user_id""",(answer_message_id,run_id))).fetchone()
    subject=((row or {}).get('content_json') or {}).get('disclosed_subject_refs',{}).get(question_id)
    if not subject or subject.get('uri')!=proposal_uri or subject.get('sha256')!=proposal_sha256:
        raise PermissionError('The answer is not bound to this displayed directory proposal')
