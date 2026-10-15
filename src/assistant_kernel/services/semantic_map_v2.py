"""One JSON Map 2.1 writer, immutable generations and ledger-backed recovery."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

import psycopg
from psycopg.rows import dict_row

from assistant_kernel.services.contracts import canonical_json, contracts
from assistant_kernel.services.document_runtime import resource_ref
from assistant_kernel.services.semantic_map import SemanticMapService


class MapGenerationChanged(ValueError):
    pass


class SemanticMapV2:
    def __init__(self,settings):
        self.settings=settings;self.compat=SemanticMapService(settings)

    def connection(self):
        s=self.settings
        return psycopg.connect(host=s.postgres_host,port=s.postgres_port,dbname=s.postgres_database,
            user=s.postgres_user,password=s.postgres_password.get_secret_value(),row_factory=dict_row)

    @staticmethod
    def now(): return datetime.now(timezone.utc).isoformat()

    def latest(self,project_id):
        path=self.compat._root(project_id)/'latest.json'
        return json.loads(path.read_bytes()) if path.is_file() else None

    @staticmethod
    def _atomic(path,value):
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
        try: temporary.write_bytes(canonical_json(value));temporary.replace(path)
        finally: temporary.unlink(missing_ok=True)

    def legacy_candidate(self,project_id):
        """Preserve all old component identities and the exact richer legacy source."""
        old=self.compat.load(project_id);now=self.now();root=self.compat._root(project_id)
        encoded=canonical_json(old);digest=hashlib.sha256(encoded).hexdigest()
        archive=root/'legacy'/(digest+'.json')
        if not archive.is_file():self._atomic(archive,old)
        legacy_ref=resource_ref(kind='source',identity='legacy-map:'+digest,
            uri='vault://'+project_id+'/knowledge/map/legacy/'+digest+'.json',sha256=digest,
            purpose='complete legacy Map semantics and metadata; compatibility source only')
        kinds=set(contracts().library['$defs']['MapComponent']['properties']['kind']['enum'])
        components=[];files=[];unclassified=[legacy_ref]
        for item in old.get('files') or []:
            uri=item.get('uri') or 'workspace://'+item['root_key']+'/'+item['path']
            if not item.get('sha256'):
                unclassified.append(resource_ref(kind='source',identity='legacy-file-'+hashlib.sha256(uri.encode()).hexdigest(),
                    uri=uri,purpose='Legacy file lacks a verified source hash'));continue
            ref=item.get('document_ref');lines_known=isinstance(item.get('lines_added'),int) and isinstance(item.get('lines_deleted'),int)
            files.append({'file_id':item.get('file_id') or 'file-'+hashlib.sha256(uri.encode()).hexdigest()[:24],
                'uri':uri,'root_key':item['root_key'],'path':item['path'],'kind':'document' if ref else 'file',
                'purpose':item.get('purpose') or '待分类','purpose_status':'classified' if item.get('purpose') and item['purpose']!='待分类' else 'unclassified',
                'component_ids':item.get('component_ids') or [],'sha256':item['sha256'],'last_changed_at':item.get('last_changed_at'),
                'last_observed_at':item.get('last_observed_at') or old.get('generated_at') or now,
                'last_change_id':item.get('last_change_id'),'lines_added':item.get('lines_added') if lines_known else None,
                'lines_deleted':item.get('lines_deleted') if lines_known else None,'file_lines_added':item.get('file_lines_added'),
                'file_lines_deleted':item.get('file_lines_deleted'),'diff_ref':None,'diff_basis':item.get('diff_basis'),
                'line_count_quality':'exact' if lines_known else 'not_available','reason_ref':None,'cause_status':'unknown',
                'freshness':'unknown','document_id':ref['id'] if ref else None,'document_ref':ref,
                'semantic_head_status':'unknown' if ref else 'not_applicable','state_revision':item.get('state_revision'),'context_epoch':item.get('context_epoch')})
        for item in old.get('components') or []:
            refs=[];hashes=item.get('source_sha256')
            hashes=hashes if isinstance(hashes,list) else [hashes]
            for i,ref in enumerate(item.get('source_refs') or []):
                if isinstance(ref,dict):refs.append(deepcopy(ref));continue
                refs.append(resource_ref(kind='source',identity='source-'+hashlib.sha256(str(ref).encode()).hexdigest(),
                    uri=str(ref),sha256=hashes[i] if i<len(hashes) else None,purpose=item['purpose']))
            refs.append(legacy_ref)
            relevance={}
            for phase,alias in [('requirements','requirements'),('design','requirements'),('build','implementation'),('audit','review'),('operate','operation')]:
                value=(item.get('relevance') or {}).get(phase,(item.get('relevance') or {}).get(alias,'low'))
                relevance[phase]='not_required' if value=='archive' else value
            components.append({'component_id':item['component_id'],'kind':item.get('kind') if item.get('kind') in kinds else 'module',
                'purpose':item['purpose'],'status':item.get('status') if item.get('status') in {'active','draft','archived','withdrawn','unknown'} else 'unknown',
                'source_refs':refs,'effective_ref':item.get('effective_ref'),'applies_to':item.get('applies_to') or [],
                'dependencies':item.get('dependencies') or [],'related_ids':item.get('related_ids') or [],
                'updated_at':item.get('updated_at') or old.get('generated_at') or now,'relevance':relevance,
                'criticality':item.get('criticality')})
        return {'schema_version':'semantic-map.v2','map_schema_version':'2.1','project_id':project_id,
            'base_pack_version':old.get('base_pack_version') or 'assistant-base-pack.v1','generated_at':now,
            'authority':{'navigation':'derived','runtime_state':'postgresql','implementation':'source_and_artifacts','external_effects':'receipts'},
            'components':components,'files':files,'unclassified_sources':unclassified,
            'coverage':{'status':'unknown','discovery_roots':[],'indexed_file_count':0,'unclassified_file_count':0,
                'excluded_categories':[],'notes':['Legacy source retained; initial source reconciliation is required.']},
            'source_basis':{'source_batch_id':'legacy:'+digest,'task_versions':[],'source_refs':[legacy_ref],'as_of':now},
            'coverage_policy':{'scope':'project_business_and_registered_external','internal_default_included':True,
                'external_root_refs':[],'external_assessment_refs':[],'declared_exclusions':[],
                'candidate_source':'task_manifest','self_indexing':'stable_component_reference',
                'external_detection':'entry_read_publish_reconcile','partial_reconcile':'retain_unvisited_records',
                'full_scan':'initial_explicit_refresh_or_structure_change'}}

    def publish_locked(self,project_id,index,*,expected_generation,source_change_ids):
        contracts().validate('Active_MapIndex',index)
        if index['project_id']!=project_id:raise PermissionError('Map index belongs to another Project')
        previous=self.latest(project_id);actual=previous['generation_id'] if previous else None
        if actual!=expected_generation:raise MapGenerationChanged('Map publication head changed; merge and rebuild')
        encoded=canonical_json(index);digest=hashlib.sha256(encoded).hexdigest();identity='gen-'+digest[:24]
        root=self.compat._root(project_id);generation=root/'generations'/identity
        generation.mkdir(parents=True,exist_ok=True)
        manifest={'schema_version':'semantic-map-manifest.v2','project_id':project_id,'generation_id':identity,
            'status':'complete','index':'generations/'+identity+'/index.json','index_format':'json','index_sha256':digest,
            'generated_at':index['generated_at'],'source_basis':index['source_basis'],
            'source_change_ids':sorted(set(source_change_ids)),'previous_generation_id':actual}
        contracts().validate('MapManifest',manifest)
        for name,value in [('index.json',index),('manifest.json',manifest)]:
            target=generation/name
            if target.is_file() and target.read_bytes()!=canonical_json(value):raise ValueError('Immutable Map generation collision')
            if not target.is_file():self._atomic(target,value)
        latest={'schema_version':'semantic-map-latest.v2','project_id':project_id,'generation_id':identity,
            'manifest':'generations/'+identity+'/manifest.json',
            'manifest_sha256':hashlib.sha256(canonical_json(manifest)).hexdigest(),'complete':True,'updated_at':index['generated_at']}
        contracts().validate('MapLatest',latest)
        self._atomic(root/'latest.json',latest)
        return latest

    def repair_marks_locked(self,connection,project_id):
        latest=self.latest(project_id)
        if not latest or latest['schema_version']!='semantic-map-latest.v2':return 0
        # This verifies both manifest and index before advancing derived DB progress.
        self.compat.current_ref(project_id)
        manifest=json.loads((self.compat._root(project_id)/latest['manifest']).read_bytes())
        ids=manifest['source_change_ids']
        cursor=connection.execute("""UPDATE core.project_file_changes SET map_applied_generation=%s
            WHERE project_id=%s AND change_id=ANY(%s) AND map_applied_generation IS NULL""",
            (latest['generation_id'],project_id,ids))
        return cursor.rowcount

    def _file(self,project_id,uri,roots,change,previous):
        from assistant_kernel.services.workspace_scope import WorkspaceScopeService
        if change.get('scope_kind')=='artifact' and change.get('actor')=='kernel' and change.get('document_ref'):
            ref=change['document_ref'];prefix='vault://'+project_id+'/'
            if not ref['uri'].startswith(prefix) or uri!='workspace://project/'+ref['uri'][len(prefix):]:
                raise PermissionError('Map artifact identity differs from the verified change')
            target=self.compat.runtime.resolve_vault_file(project_id,ref['uri'][len(prefix):])
        else:
            try: _,target,_=WorkspaceScopeService(self.settings).resolve_uri(roots,uri)
            except PermissionError:
                return {**previous,'freshness':'unknown'} if previous else None
        if not target.is_file():
            if change.get('operation')=='delete' and change.get('after_sha256') is None and change.get('effect_status')=='verified':return None
            if previous:return {**previous,'freshness':'stale','last_observed_at':self.now()}
            return None
        digest=hashlib.sha256(target.read_bytes()).hexdigest();root_key,path=uri[len('workspace://'):].split('/',1)
        if not change and previous and previous['sha256']==digest:return previous
        matches=change.get('after_sha256')==digest
        exact=matches and isinstance(change.get('lines_added'),int) and isinstance(change.get('lines_deleted'),int)
        reason_refs=change.get('reason_source_refs') or []
        backed=matches and change.get('cause_status')=='source_backed' and bool(reason_refs) and isinstance(reason_refs[0],dict)
        diff=change.get('diff_ref') if matches else None
        if isinstance(diff,str):
            prefix='vault://'+project_id+'/'
            if not diff.startswith(prefix):raise PermissionError('Map diff belongs to another Project')
            data=self.compat.runtime.resolve_vault_file(project_id,diff[len(prefix):]).read_bytes()
            diff=resource_ref(kind='source',identity='diff-'+hashlib.sha256(data).hexdigest(),uri=diff,
                sha256=hashlib.sha256(data).hexdigest(),purpose='verified change diff')
        document_ref=change.get('document_ref') or (previous or {}).get('document_ref')
        return {'file_id':(previous or {}).get('file_id') or 'file-'+hashlib.sha256(uri.encode()).hexdigest()[:24],
            'uri':uri,'root_key':root_key,'path':path,'kind':'document' if document_ref else 'file',
            'purpose':change.get('purpose') or (previous or {}).get('purpose') or '待分类',
            'purpose_status':'classified' if change.get('purpose') or (previous or {}).get('purpose_status')=='classified' else 'unclassified',
            'component_ids':(previous or {}).get('component_ids') or [],'sha256':digest,
            'last_changed_at':change.get('occurred_at') if matches else None,'last_observed_at':self.now(),
            'last_change_id':change.get('change_id'),'lines_added':change.get('lines_added') if exact else None,
            'lines_deleted':change.get('lines_deleted') if exact else None,'file_lines_added':change.get('file_lines_added') if matches else None,
            'file_lines_deleted':change.get('file_lines_deleted') if matches else None,'diff_ref':diff,
            'diff_basis':change.get('diff_basis') if matches else None,
            'line_count_quality':'exact' if exact else 'observed_mismatch' if change and not matches else 'not_available',
            'reason_ref':change['reason_source_refs'][0] if backed else None,'cause_status':'source_backed' if backed else 'unknown',
            'freshness':'current','document_id':document_ref['id'] if document_ref else None,'document_ref':document_ref,
            'semantic_head_status':'accepted' if change.get('scope_kind')=='artifact' and change.get('actor')=='kernel' else 'unknown' if document_ref else 'not_applicable',
            'state_revision':change.get('state_revision'),'context_epoch':change.get('context_epoch')}

    def refresh(self,project_id,*,roots,changes=(),expected_generation=None,full_scan=False):
        import fcntl
        root=self.compat._root(project_id);root.mkdir(parents=True,exist_ok=True)
        with (root/'map-writer.lock').open('a+b') as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
            try:
                with self.connection() as connection:
                    self.repair_marks_locked(connection,project_id);connection.commit()
                    pending=connection.execute("""SELECT record_json FROM core.project_file_changes
                        WHERE project_id=%s AND scope_kind<>'candidate' AND map_applied_generation IS NULL
                        ORDER BY recorded_at,change_id""",(project_id,)).fetchall()
                    facts=[row['record_json'] for row in pending]
                    known={item['change_id'] for item in facts}
                    if any(item['change_id'] not in known and not connection.execute(
                            'SELECT 1 FROM core.project_file_changes WHERE project_id=%s AND change_id=%s',
                            (project_id,item['change_id'])).fetchone() for item in changes):
                        raise PermissionError('Map changes must be committed immutable ledger facts')
                    latest=self.latest(project_id);actual=latest['generation_id'] if latest else None
                    rebased=expected_generation is not None and expected_generation!=actual
                    if latest and latest['schema_version']=='semantic-map-latest.v2' and not facts and not full_scan:
                        (root/'stale.json').unlink(missing_ok=True)
                        return {**latest,'rebased':rebased,'recovered_progress':True}
                    index=self.compat.load(project_id) if latest and latest['schema_version']=='semantic-map-latest.v2' else self.legacy_candidate(project_id)
                    files={item['uri']:item for item in index['files']};visited=set()
                    if full_scan:
                        scan_uris=[f'workspace://{r.key}/' for r in roots if r.key not in {'candidate','run_work'}]
                        scan_uris += [uri for uri,item in files.items() if item['kind']=='document']
                        scanned=self.compat.scan_files(project_id,roots,source_uris=scan_uris,exact_sources=True,file_limit=None)
                        for uri in scanned:
                            files[uri]=self._file(project_id,uri,roots,{},files.get(uri));visited.add(uri)
                        # A full scan removes only ordinary entries in the actual scanned roots.
                        keys={r.key for r in roots}
                        files={uri:item for uri,item in files.items() if item['root_key'] not in keys or uri in visited or item['kind']=='document'}
                    for change in facts:
                        if change.get('accepted_head_guard'):
                            # Accepted-document projections of the retired control core: their owners were
                            # archived, so they stay historical facts and never replace the current map.
                            continue
                        uri=change['target_uri'];previous=files.get(uri)
                        entry=self._file(project_id,uri,roots,change,previous)
                        if entry:
                            if entry['document_id']:
                                files={u:f for u,f in files.items() if f.get('document_id')!=entry['document_id'] or u==uri}
                            files[uri]=entry
                        elif change['operation']=='delete' and change.get('effect_status')=='verified' and change.get('after_sha256') is None:
                            files.pop(uri,None)
                        else:
                            index['unclassified_sources'].append(resource_ref(kind='source',identity=change['change_id'],
                                uri='db://assistant/project_file_changes/'+change['change_id'],
                                purpose='Changed source unresolved in current readable scope; retained ledger fact'))
                    index['files']=[files[uri] for uri in sorted(files)];index['generated_at']=self.now()
                    index['coverage'].update(indexed_file_count=len(files),
                        unclassified_file_count=sum(f['purpose_status']=='unclassified' for f in files.values()))
                    if full_scan:
                        index['coverage']['status']='partial'
                        index['coverage']['discovery_roots']=[{'root_key':r.key,'grant_ref':None,'managed_entrypoints':[],
                            'coverage':'complete' if r.key=='project' else 'partial'} for r in roots]
                        index['coverage']['notes']=['Managed source scan completed; transient exclusions and external coverage remain explicit.']
                    refs=[ref for change in facts for ref in change.get('reason_source_refs') or []]
                    versions={item['task_id']:item for item in index.get('source_basis',{}).get('task_versions',[])}
                    index['source_basis']={'source_batch_id':'map:'+hashlib.sha256(canonical_json(sorted(known))).hexdigest(),
                        'task_versions':list(versions.values()),'source_refs':refs,'as_of':index['generated_at']}
                    published=self.publish_locked(project_id,index,expected_generation=actual,source_change_ids=known)
                    self.repair_marks_locked(connection,project_id);connection.commit()
                    (root/'stale.json').unlink(missing_ok=True)
                    return {**published,'rebased':rebased,'file_count':len(files)}
            except Exception as exc:
                self.compat.mark_stale(project_id,reason='Map generation/ledger progress pending: '+type(exc).__name__)
                raise
