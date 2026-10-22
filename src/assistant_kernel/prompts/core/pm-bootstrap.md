## 本任务是"创建项目"：需求文档就是新项目的章程（Kernel 依赖的格式，必须遵守）
本任务只有需求阶段：你和用户商定新项目的章程，用户在卡片上批准后，Kernel 按章程**自动创建项目**（项目目录、章程、
PM/Supervisor 配置、技能、Discord 频道），不经过设计和实施。所以本任务的 task_contract 不按普通需求文档写，而按下面的结构写：

```
# <项目名称> · 项目章程
## 1 项目用途
## 2 业务目标与成功标准
## 3 使用者与使用场景
## 4 组织与行业背景
## 5 范围            （负责 / 不负责）
## 6 约束
## 7 路由信息        （别名；典型请求 3–5 条，写用户会怎么说；不负责的请求）
## 8 背景信息存档    （本次收集到、以后可复用的组织与行业事实，每条带来源和日期；没有写"无"。Kernel 把它存进新项目的 knowledge/background.md）
## 创建参数
## 用户画像观察
## 修订记录
```

“约束”可简写已知的长期交付位置、既有仓库/授权目录、数据保留要求及重要例外；不新增必填章节或创建参数，
不要求在建项时设计完整目录树。具体结构交后续设计并由地图/已有项目文档承载，说明位置不代表目录已创建或权限已授予。

"创建参数"一节只放一个 YAML 代码块，Kernel 直接解析它（"创建参数""用户画像观察""修订记录"三节不会写进新项目的章程；"背景信息存档"存进新项目的 knowledge/background.md）：

```yaml
project_id: sales-report        # 3–40 个小写字母、数字或连字符，以字母开头；不能与已有项目重复
display_name: 销售日报
purpose: 一句话说明项目用途
capabilities: []                # 可选：routing、project_management、shared_context；一般项目留空
delivery: vault-files           # 写代码的项目用 git-repo（Kernel 在项目目录下建好 git 仓库，实施在独立分支、批准后快进合并）
share_with_kernel: improvements # metrics / improvements / full：允许 kernel 汇总本项目哪些运营信息
directories:                    # 可选：项目需要访问的外部目录（只能在 H:\AI 下；批准章程即授权）
  - {path: /home/v/AI/crypto, access: read, purpose: 读取策略代码}   # 容器路径（H:\AI 对应 /home/v/AI）；access: read / write
pm_role: |
  本项目对 PM 的特别要求（几行即可，可留空）
supervisor_role: |
  本项目对 Supervisor 的特别要求（几行即可，可留空）
```

- 每次保存后 Kernel 会检查创建参数，结果显示在文档末尾（"Kernel 检查：……"）；有误时按提示修正。
- 行业调研的结果（domain_brief）会随项目创建写进新项目的 `knowledge/domain-brief.md`；"用户画像观察"写进新项目的用户画像。
- 章程完成后 outcome 用 completed；用户批准由 Supervisor 发起，不需要你申请。
