"""Build the complete v1.2 public template candidate from locked design sources.

Writing this package does not adopt it for any existing Project or Task.
"""
import argparse
import hashlib
import json
from pathlib import Path

BASELINE='f9ae591846cc24c57687dd9bc35c8fa0f940668a83bcf1e66ff1f0414a4a3b29'
PREFIX='fixture://v1.2/templates/'


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--design-root',type=Path,required=True)
    args=parser.parse_args()
    report=args.design_root/'Kernel 基础版本统一设计报告候选稿 v1.2.md'
    if hashlib.sha256(report.read_bytes()).hexdigest()!=BASELINE:raise ValueError('Design baseline changed')
    sources=json.loads((args.design_root/'contracts/source-materials.json').read_text('utf-8'))
    root=Path(__file__).resolve().parents[1]/'src/assistant_kernel/prompts/v12'
    additions={
        'roles/common-control.md':'业务程序问题使用 runtime_both：Supervisor 主入口与 PM 镜像均可完整作答；维护问题使用 runtime_pm_primary，在 PM 作答。',
        'roles/supervisor.md':'普通业务程序的 needs_input 在 Supervisor 主入口与 PM 镜像均可完整作答；维护问题仍在 PM 作答，同一问题仅接受一次。',
        'skills/assistant-interaction-followup/SKILL.md':'runtime_business 使用 runtime_both，Supervisor 主入口与 PM 镜像同样可答；runtime_maintenance 使用 runtime_pm_primary。两入口引用同一内容版、草稿和答案。',
        'skills/assistant-python-execution/SKILL.md':'CPU 池大于零必须声明实际 start_method；包括混合 I/O 程序。identity_unknown 保留已知 PID、未知身份字段为 null，不凭不完整身份杀进程或自动重跑，先对账。',
        'skills/assistant-service-lifecycle/SKILL.md':'所有工作含纯 I/O 和待答项都先登记实例 work_control；维护闸门与工作入场共用 Unit 锁。CPU/模型预留属于子记录，不另造任务系统；CPU 池声明实际 start_method。',
        'skills/assistant-system-config/SKILL.md':'保留 providers.chat_models 的非秘密模型与端点字段。明确空模型名/端点为 null 未配置，不回填默认模型。无价格来源时 pricing_verification 为 unverified，不据此报告确定费用。',
        'skills/assistant-project-map-navigation/SKILL.md':'D10（用户确认简化）：现阶段不实现额外的运行中目录增长阈值通知、watcher 或去重状态。保留创建/扩目录时的有界规模展示和覆盖标记；体量阈值只是提示，不限制目录约定或正常执行。按需读取，遇到实际扫描/读取能力缺口再明确反馈。原文“明显增长提醒”在本次实施中暂缓。',
    }
    entries=[]
    for uri,body in sources.items():
        if not uri.startswith(PREFIX) or not uri.endswith('.md'):continue
        relative=uri[len(PREFIX):]
        path=root/relative
        if root.resolve() not in path.resolve().parents:raise ValueError('Template path escapes package')
        original=body.encode('utf-8')
        body=body.replace('输出Active_AgentResult','输出V12_Active_AgentResult')
        if relative in additions:body=body.rstrip()+'\n\n## 已确认实施契约修订\n'+additions[relative]+'\n'
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(body,encoding='utf-8',newline='\n')
        entries.append({'path':relative,'design_source_uri':uri,'source_sha256':hashlib.sha256(original).hexdigest(),
                        'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    library=root.parents[1]/'contracts/schema-library-v12.json'
    schema=root/'schemas/schema-library.json';schema.parent.mkdir(parents=True,exist_ok=True);schema.write_bytes(library.read_bytes())
    output={'$schema':'https://json-schema.org/draft/2020-12/schema','$ref':'schema-library.json#/$defs/V12_Active_AgentResult'}
    (root/'schemas/agent-result.schema.json').write_text(json.dumps(output,indent=2)+'\n',encoding='utf-8',newline='\n')
    profiles=json.loads((args.design_root/'contracts/profiles.json').read_text('utf-8'))
    def rewrite(value):
        if isinstance(value,dict):
            uri=value.get('uri')
            if isinstance(uri,str) and uri.startswith(PREFIX):
                relative=uri[len(PREFIX):];target=root/relative
                if not target.is_file():raise ValueError('Missing template reference: '+relative)
                sha=hashlib.sha256(target.read_bytes()).hexdigest()
                value.update(uri='kernel://template-assets/'+sha+'/'+relative,sha256=sha,
                             id='v12-template/'+relative,purpose='fixed published template asset; not business approval')
            for item in value.values():rewrite(item)
        elif isinstance(value,list):
            for item in value:rewrite(item)
    rewrite(profiles)
    (root/'profiles.json').write_text(json.dumps(profiles,ensure_ascii=False,sort_keys=True,indent=2)+'\n',encoding='utf-8',newline='\n')
    files={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file() and p.name!='manifest.json'}
    digest=hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    manifest={'schema_version':'kernel-template-bundle.v1','design_revision':'1.2','baseline_sha256':BASELINE,
              'bundle_sha256':digest,'files':files,'design_sources':entries,'adopted_projects':[],
              'contract_library_sha256':hashlib.sha256(library.read_bytes()).hexdigest()}
    (root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,sort_keys=True,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({'template_files':len(files),'role_skill_sources':len(entries),'bundle_sha256':digest,'adopted_projects':[]}))


if __name__=='__main__':main()
