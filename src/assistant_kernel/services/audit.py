from __future__ import annotations


AUDIT_SCHEMA_VERSION = "session-impact-audit.v2"
LEGACY_AUDIT_SCHEMA_VERSIONS = frozenset({"post-change-audit.v1"})

AUDIT_READ_ONLY_TOOLS = frozenset({
    "get_authorized_workspace",
    "list_authorized_tree",
    "read_authorized_file",
    "list_projects",
    "list_project_agents",
    "current_conversation_route",
    "list_recent_history",
    "get_project_map", "query_project_map", "read_map_component",
    "list_source_discovery_roots",
    "inspect_source_path",
    "run_status",
    "get_stage_handoff",
    "list_stage_artifacts",
    "read_stage_artifact",
})


def audit_read_only_tools(tools: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(tool for tool in tools if tool in AUDIT_READ_ONLY_TOOLS)


def is_audit_schema(value: str) -> bool:
    return value == AUDIT_SCHEMA_VERSION or value in LEGACY_AUDIT_SCHEMA_VERSIONS


def build_audit_prompt(project_id: str, mode: str = "full") -> str:
    normalized = (mode or "full").strip().lower()
    if normalized not in {"quick", "full"}:
        raise ValueError("audit mode must be quick or full")
    depth = (
        "快速模式：优先检查本轮成果本身及最直接的上下游和共享状态。"
        if normalized == "quick"
        else
        "完整模式：沿本轮修改实际触及的依赖链，检查所有必要的上游、下游和共享机制。"
    )
    return f"""你正在执行 Kernel 固化的当前 Session 最新轮次成果只读审计。

审计协议：{AUDIT_SCHEMA_VERSION}
审计模式：{normalized}
当前 Project：{project_id}

审计对象：当前正在交互的 Session 中最近一轮已经完成的改进成果。“最近一轮”从用户最近一次明确提出修改、修复或实现要求开始，到助手最近一次声明完成为止。

审计目标：判断这一轮成果是否满足用户要求，并在放回现有整体架构后仍然闭环、自洽、无明显副作用。审计应从当前 Session 的本轮证据出发，只沿本轮修改实际影响到的依赖关系扩展，不得无差别重新审计整个系统。{depth}

强制边界：
1. 只读检查。不得修改代码、配置、数据库、频道、权限、消息状态或外部系统；不得启动 Implementation、Publisher 或其他写操作。
2. 优先使用当前 Session 已有的用户要求、消息、工具回执、修改文件、测试结果和外部操作回执。只有某项完成声明无法由这些证据确认时，才进行与该声明直接相关的最小只读检查。
3. 允许检查全局影响，但必须能说明它与本轮修改之间的调用、数据、状态、配置、权限或恢复依赖；不得扫描无依赖关系的其他 Project、历史任务或模块。
4. 严格区分：已实现、已部署、自动测试通过、真实 E2E 已验证、仍未验证。证据不足时明确写“未验证”，不得推测为通过。

核查重点：
- 需求覆盖：本轮用户要求是否被正确理解并全部满足，有无遗漏、越界或错误假设。
- 成果真实性：本轮修改、配置、部署、测试和完成声明是否相互一致。
- 局部闭环：本轮入口、状态转换、执行、回复、失败反馈和恢复路径是否首尾对应。
- 依赖驱动的全局影响：只检查本轮实际触及的上游调用者、下游消费者、共享数据结构、配置、权限、Session、Run、路由、并发锁、幂等、恢复机制和渠道备用路径。
- 连锁副作用：检查本轮是否导致其他 Project、频道、Session、Worker 或历史任务串线、重复执行、状态冲突、权限扩大或错误恢复。
- 必要场景：只对与本轮变化相关的正常路径、重复调用、部分成功、执行中断、进程重启和外部对象变化进行检查。

输出格式：
1. 结论：PASS、CONDITIONAL PASS 或 FAIL。
2. 最近一轮需求：概括本轮真正需要完成什么。
3. 成果与证据：列出本轮修改及其已实现/已部署/测试通过/真实 E2E/未验证状态。
4. 局部闭环：说明本轮业务流程和状态转换是否完整。
5. 全局影响：只列沿实际依赖关系发现的兼容性、连锁影响和无副作用证据。
6. 问题清单：按 P0/P1/P2 排序，写明触发条件、后果、证据和最小修复方向。
7. 最小后续建议：只列必要修复或验收步骤；本次审计不得直接实施修复。
"""
