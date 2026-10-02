---
name: assistant-source-scope
description: 将用户指定的父目录激活为Project递归授权上限，并用每Run Context View按需发现和读取相关内容；不把软过滤误当权限。
metadata:
  version: "3.0.0"
---

# Project递归授权与上下文视图

## 任务目标

用户决定Project可以访问哪些父目录，Kernel固定真实路径、授权来源和Run快照。用户明确指定一个父目录后，该目录下现有及以后新增的全部子目录和文件都属于同一份Project级递归文件Grant。Grant是权限上限；每个Run的Context View只是减少无关目录展示和扫描，不缩小Grant，也不新增权限。

## 授权规则

1. 新Project创建请求已经列出外部父目录时，把这些目录直接放入`propose_project.workspace_roots`；用户批准Project提案后，Kernel在创建Project时同时激活目录Grant，首个PM Run即可继承。
2. 现有Project运行中，用户明确给出父目录即构成该目录的授权请求。调用`propose_source_scope`时只提交`discovery_id`、`relative_path`、用途和可选稳定`root_key`；不得提交或设计`includes`、`excludes`、精确文件列表或gated子范围。
3. 用户明确说出路径的当前消息可使用`authorization_mode=explicit_request`直接激活；只有路径由Agent建议、系统handoff未关联原始用户消息或用户只说“其他需要的目录”而未给出具体父目录时，才展示具体父目录并等待一次批准。
4. 父目录Grant自动覆盖其下旧的子目录Grant；Kernel应撤销被父级包含的旧Grant，不要求用户先撤销或逐项重新批准。
5. 授权不依赖目录大小、文件数量、扩展名、元数据扫描是否截断或目录中是否存在配置、日志、数据库、行情、二进制及运行文件。不得因为这些因素缩小用户指定的父目录。
6. 权限从下一次Run生效；激活完成后报告实际父目录、Grant ID、receipt和被父级替代的旧Grant。

## Context View规则

1. `get_authorized_workspace`只返回Root摘要、Project内部目录和`source-map.yaml`固定的初始路径；外部Root没有展开不代表未授权。
2. 需要外部事实时，优先调用`list_authorized_tree(root_key, relative_path, max_depth, limit)`浏览一个精确Root/子路径，再用`read_authorized_file`读取最少必要文件。不得先用Shell递归扫描整个父目录。
3. `source-map.yaml`只维护`mode`、`entrypoints`和`soft_excludes`：
   - `live`用于持续变化的代码或工作目录；
   - `discovery`只在需要时浏览；
   - `lazy_snapshot`用于首次使用时复制稳定参考资料并记录来源哈希；
   - `indexed`用于大型文档或数据，通过解析和检索取得片段。
4. `soft_excludes`只影响默认目录展示。当前任务明确需要其中某个路径时，可以对该精确路径调用`list_authorized_tree(..., include_soft_excluded=true)`；这不是扩权，不得借此无目的展开整个被忽略目录。
5. Agent可以在正式Requirements/Implementation流程中提出或修改Context View，但不得通过修改`source-map.yaml`激活、扩大、撤销Project Grant。Grant只能通过Kernel授权工具改变。
6. 新Project只创建Source Map，不复制或移动整个外部目录。稳定参考文件在首次实际需要时才做可追溯快照；活跃代码保持原地；密钥和控制面文件永不复制进Project。

## 使用行为

技术权限不等于当前任务需要。即使父目录已经完整授权，也必须强烈遵守：

- 只访问、修改或执行完成当前明确任务实际需要的文件和文件夹；优先复用Context View，缺少证据时再按Root和相对路径逐步扩大浏览。
- 不得无目的遍历整个目录，不得顺手清理、重构、删除或修复不相关内容。
- 发现相关但不属于当前任务的内容，只报告发现，不直接处理。
- 如果准备访问或执行的内容可能敏感、高影响或与任务关系不够明确，在当前聊天中说明精确路径、原因、计划动作和可能影响，并询问用户。用户明确同意后继续，不再为其子目录或单个文件重复申请。
- 修改即时生效的文件时保留基线、实际差异、测试和必要回退证据；执行脚本记录脚本、命令、退出码和结果。

## 凭据

需要账号、API Key、Token或密码时，先按`assistant-project-credentials`检查Project凭据。缺失时必须在当前聊天中向用户发问，并使用Discord或当前通道提供的一对一凭据卡片收集；不得因为需要凭据而把已经可以推进的Project工作长期阻塞。

## 边界

- 只能激活Kernel发现目录中的真实父目录，解析后的路径不得越出用户指定的父目录。
- Project文件Grant不代表用户已经要求执行实盘交易、发送订单或其他不同业务动作；这些动作仍以当前任务原话和Project业务边界为准。
- 工具没有返回成功回执时，不得声称授权已经生效。
- 不得为减少Token而移动、隐藏或重命名用户原文件；备份和参考快照只能复制并保留来源、哈希和时间。
