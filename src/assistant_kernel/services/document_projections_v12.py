"""Accepted document references and their derived Map work share the owner commit."""
import hashlib

from assistant_kernel.repositories import change_ledger
from assistant_kernel.services.document_runtime import TaskDocumentService
from assistant_kernel.services.project_changes import ProjectChangeService


class DocumentProjectionsV12:
    def __init__(self,settings,repository):
        self.settings=settings;self.repository=repository;self.documents=TaskDocumentService(settings)

    async def record_locked(self,connection,*,project,owner,refs,source_refs,guard='task'):
        if not await change_ledger.enabled(connection):return None # Compatibility-only isolated v7 readers.
        changes=[]
        for ref in refs:
            document=self.documents.read(project,ref);prefix='vault://'+project+'/'
            if not ref['uri'].startswith(prefix):raise PermissionError('Accepted document belongs to another Project')
            path=self.documents.runtime.resolve_vault_file(project,ref['uri'][len(prefix):])
            if hashlib.sha256(path.read_bytes()).hexdigest()!=ref['sha256']:raise ValueError('Accepted document bytes changed')
            changes.append({'target_uri':'workspace://project/'+ref['uri'][len(prefix):],'document_ref':ref,
                'operation':'discovered','before_sha256':None,'after_sha256':ref['sha256'],
                'purpose':document['schema_version'],'state_revision':int(owner['request_json'].get('state_revision') or 0),
                'context_epoch':int(owner['request_json'].get('supervisor_context_epoch') or 0),
                'accepted_head_guard':{'kind':guard,'owner_id':str(owner['run_id'])}})
        if not changes:return None
        basis='|'.join(sorted(ref['sha256'] for ref in refs))+':'+str(owner['request_json'].get('state_revision') or 0)+':'+guard
        batch='accepted-documents:'+hashlib.sha256(basis.encode()).hexdigest()
        return await ProjectChangeService(self.settings,self.repository.database).capture_batch(project,batch_id=batch,roots=[],
            changes=changes,task_id=str(owner['run_id']),run_id=str(owner['run_id']),actor='kernel',
            reason='Current accepted document revision; model findings and business acceptance remain separately evidenced',
            reason_source_refs=source_refs,exact_time=False,scope_kind='artifact',connection=connection)
