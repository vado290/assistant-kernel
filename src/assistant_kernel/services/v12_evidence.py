"""Check references against original same-Project records and pinned bytes."""
import hashlib
import uuid

from assistant_kernel.services.document_runtime import TaskDocumentService
from assistant_kernel.services.public_background import PublicBackgroundService


def references(value):
    if isinstance(value,dict):
        if value.get('schema_version')=='resource-ref.v1':
            yield value
        else:
            for child in value.values():yield from references(child)
    elif isinstance(value,list):
        for child in value:yield from references(child)


class V12Evidence:
    def __init__(self,settings,repository):
        self.settings=settings;self.repository=repository;self.documents=TaskDocumentService(settings)

    async def verify(self,project,value,*,task=None):
        refs=list(references(value))
        revoked=PublicBackgroundService(self.settings.data_root,self.settings).revoked_source_message_ids()
        async with self.repository.database.connection() as connection:
            for ref in refs:
                uri=str(ref.get('uri') or '')
                if ref['kind']=='document':
                    self.documents.read(project,ref)
                elif uri.startswith('db://assistant/'):
                    parts=uri[len('db://assistant/'):].split('/')
                    if len(parts)!=2:raise ValueError('Evidence record URI has no concrete identity')
                    table=parts[0];identity,separator,fragment=parts[1].partition('#')
                    key=identity if table in {'model_invocations','project_file_changes'} else str(uuid.UUID(identity))
                    columns={'messages':'message_id','runs':'run_id','model_invocations':'invocation_id',
                        'project_file_changes':'change_id','service_instances':'instance_id','schedule_ticks':'tick_id'}
                    if table not in columns:raise PermissionError('This record type has no verified evidence reader')
                    row=await (await connection.execute('SELECT * FROM assistant.'+table+' WHERE '+columns[table]+'=%s',(key,))).fetchone()
                    if not row or row.get('project_id')!=project:raise PermissionError('Evidence record is missing or belongs to another Project')
                    if separator:
                        if table!='service_instances' or not fragment.startswith('work/'):raise PermissionError('Unsupported evidence fragment')
                        if not any(hashlib.sha256(x.encode()).hexdigest()==fragment.removeprefix('work/') for x in row['state_json'].get('work_records',{})):
                            raise PermissionError('Service work evidence is missing')
                    if table=='messages':
                        body=row['content_json'] or {}
                        if key in revoked or body.get('context_revoked') or body.get('source_revoked'):
                            raise PermissionError('Evidence source has been revoked')
                    if table=='runs' and row.get('cancel_requested_at') and ref.get('purpose')=='required child result':
                        raise PermissionError('Required child was cancelled')
                elif uri.startswith('vault://'):
                    prefix='vault://'+project+'/'
                    if not uri.startswith(prefix):raise PermissionError('Evidence file belongs to another Project')
                    path=self.documents.runtime.resolve_vault_file(project,uri[len(prefix):])
                    if not path.is_file():raise FileNotFoundError('Evidence file does not exist')
                    if not ref.get('sha256') or hashlib.sha256(path.read_bytes()).hexdigest()!=ref['sha256']:
                        raise PermissionError('Evidence file has no matching pinned hash')
                elif uri.startswith('workspace://'):
                    if not task or not ref.get('sha256'):raise PermissionError('Workspace evidence needs a Task and pinned hash')
                    from assistant_kernel.services.workspace_scope import WorkspaceScopeService
                    scopes=WorkspaceScopeService(self.settings);roots=scopes.resolve(project,str(task['run_id']),'implementation')
                    _,path,_=scopes.resolve_uri(roots,uri)
                    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=ref['sha256']:
                        raise PermissionError('Workspace evidence differs from its pinned version')
                else:
                    raise PermissionError('Evidence needs an original readable record or pinned local receipt')
        return refs
