"""Archive unused child/Operator configurations without changing business Tasks."""
from __future__ import annotations
import asyncio
import hashlib
import json
import shutil
from pathlib import Path

import yaml
from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.storage import ProjectRuntimeManager
from assistant_kernel.services.semantic_map import SemanticMapService
from assistant_kernel.services.runtime_catalog import RuntimeCatalogService


async def retire(settings: KernelSettings) -> dict:
    repo=CoreRepository(settings)
    async with repo.database.connection() as connection:
        rows=list(await (await connection.execute("""SELECT run_id,status FROM assistant.runs
            WHERE (agent_key LIKE '%%:child' OR agent_key LIKE '%%:operator'
                   OR request_json->>'interaction_kind'='child_task' OR status='waiting_children')
              AND status NOT IN ('succeeded','failed','cancelled','dead_letter')""",())).fetchall())
        if rows:
            raise RuntimeError(f'retired Agents have active Runs; explicit resolution required: {[str(r["run_id"]) for r in rows]}')
    backup=settings.data_root/'backups/child-removal-20261004-0248'
    runtime=ProjectRuntimeManager(settings.data_root)
    directory=settings.data_root/'config/project-directory.yaml'
    projects=(yaml.safe_load(directory.read_text()) or {}).get('projects') or []
    archived=[]
    for entry in projects:
        if entry.get('status')!='active':continue
        project_id=str(entry['project_id'])
        vault=runtime.describe(project_id).vault_root
        for name in ('child','operator'):
            source=vault/'agents'/name
            if not source.exists():continue
            if source.is_symlink() or vault.resolve() not in source.resolve().parents:
                raise RuntimeError('Agent archive source escaped its Vault')
            target=backup/'project-agents'/project_id/name
            shutil.copytree(source,target,dirs_exist_ok=True)
            for original in source.rglob('*'):
                if original.is_file() and hashlib.sha256(original.read_bytes()).digest()!=hashlib.sha256((target/original.relative_to(source)).read_bytes()).digest():
                    raise RuntimeError('Agent archive verification failed')
            shutil.rmtree(source)
            archived.append(f'{project_id}:{name}')
        # Update only navigation entries that refer to the removed Agents.
        index=vault/'knowledge/map/index.yaml'
        if index.exists():
            data=yaml.safe_load(index.read_text()) or {}
            old=data.get('components') or []
            components=[]
            for item in old:
                item=dict(item)
                refs=list(item.get('source_refs') or [])
                if item.get('component_id') in {'agent.project-child','agent.project-operator'}:continue
                item['source_refs']=[ref for ref in refs if '/agents/child/' not in ref and '/agents/operator/' not in ref]
                if item.get('component_id')=='flow.delegation-v1':
                    item['purpose']='记录既有原生子Agent委派的请求、结果与证据，由PM在自己的Turn中汇总'
                    item['source_refs']=[f'workspace://kernel/src/assistant_kernel/services/run_processor.py']
                elif item.get('component_id')=='flow.supervisor-barrier-v1':
                    item['purpose']='PM Turn结算后由平级Supervisor评估Action Plan，再通过Kernel自动继续或等待用户'
                components.append(item)
            if components!=old:
                target=backup/'project-maps'/project_id
                target.mkdir(parents=True,exist_ok=True)
                shutil.copy2(index,target/'index.yaml')
                latest=index.parent/'latest.json'
                if latest.exists():shutil.copy2(latest,target/'latest.json')
                data['components']=components
                index.write_text(yaml.safe_dump(data,sort_keys=False,allow_unicode=True))
                SemanticMapService(settings).publish_current(project_id)
    # Retain external channel history. Only the removed role mapping is withdrawn.
    registry=settings.data_root/'config/discord-interfaces.json'
    if registry.exists():
        data=json.loads(registry.read_text())
        old=data.get('interfaces') or {}
        current={key:value for key,value in old.items() if value.get('role')!='operator'}
        if current!=old:
            shutil.copy2(registry,backup/'discord-interfaces.json')
            data['interfaces']=current
            registry.write_text(json.dumps(data,indent=2))
    RuntimeCatalogService(settings).refresh()
    return {'archived_agents':archived,'schema_changed':False,'business_runs_changed':False}


def restore(settings: KernelSettings) -> dict:
    backup=settings.data_root/'backups/child-removal-20261004-0248'
    runtime=ProjectRuntimeManager(settings.data_root)
    restored=[]
    for project in (backup/'project-agents').glob('*'):
        if not project.is_dir():continue
        vault=runtime.describe(project.name).vault_root
        for name in ('child','operator'):
            source=project/name
            if source.exists():
                shutil.copytree(source,vault/'agents'/name,dirs_exist_ok=True)
                restored.append(f'{project.name}:{name}')
    for project in (backup/'project-maps').glob('*'):
        target=runtime.describe(project.name).vault_root/'knowledge/map'
        for name in ('index.yaml','latest.json'):
            if (project/name).exists():shutil.copy2(project/name,target/name)
    registry=backup/'discord-interfaces.json'
    if registry.exists():shutil.copy2(registry,settings.data_root/'config/discord-interfaces.json')
    RuntimeCatalogService(settings).refresh()
    return {'restored_agents':restored}


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--restore',action='store_true')
    args=parser.parse_args()
    settings=KernelSettings.from_environment()
    print(json.dumps(restore(settings) if args.restore else asyncio.run(retire(settings))))
