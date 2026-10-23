---
name: assistant-mcp-design
description: Design or review project MCP servers and atomic tools — transport, lifecycle, manifest, agent binding, permissions, approvals and start-up verification; use only when the task involves MCP.
metadata:
  version: "1.4.0"
  load: {roles: [pm], stages: [design, build], on_demand: true}
  changelog:
    - "1.4.0 2026-10-23 Written in English, the skills' source language (ROUND6, 0.46.0)"
    - "2026-10-16 Read on demand (only name and purpose listed in the prompt); content not yet rewritten for core v6 — any old process terms and output fields in it give way to assistant-project-agent and the output requirements"
---

# MCP design

## Goal

Implement capabilities that need live systems, shared data, credentials or outside side effects as structured,
least-privilege MCP tools that can be verified at start-up, so a project's agent configuration can inject the right
servers and tools as needed. MCP provides capability boundaries; it does not carry business reasoning.

## Tool boundaries

A tool stays a clear atomic business action: for example, querying positions returns only position facts; totalling the
market value, risk exposure and the explanation belong to a skill script or the agent. Atomic does not mean splitting a
sensible query into many meaningless small tools; paging, filters and the fields a resource needs may stay together.

Each tool declares: name, purpose, input schema, output schema, its effect (read/write/external/trade), idempotency,
timeout, error types, credential references and approval requirements. Network access is available by default; a
project's file grant only authorises file operations, and an MCP tool still checks its credentials, account and trading
limits on its own. Write, publish and trade tools need a stable idempotency key, server-side permission checks and real
receipts.

## Server boundaries

- Kernel's scoped MCP keeps only control-plane tools: workspace, run, work mode, candidates, map and routing.
- A project MCP that is stateless, starts fast and serves only the current invocation uses STDIO; its source lives in the
  current project and Kernel's controlled launcher starts it; STDIO takes no port.
- A domain MCP with long connections, expensive initialisation or sharing between processes uses internal Streamable
  HTTP; Kernel resolves its endpoint from a non-sensitive reference such as `env://ENV_NAME`, and it binds only to
  loopback or an internal network.
- Split servers by dependencies, trust, lifecycle and side effects — never fold every project tool into Kernel's server,
  and never mechanically one server per tool.

## Files and configuration

A project MCP lives in `mcp/<server-id>/`. It must contain `mcp-manifest.yaml` and the actual server entry point; tools,
services and tests are created only when they have content — no empty folders. An agent configuration only references
approved servers and lists the enabled tools; it never stores credential values or arbitrary outside commands.

One candidate contains the MCP code, the manifest and every affected agent configuration. A new tool cannot change a tool
catalogue already initialised in the current LLM turn; after the release, the next turn's resolver produces a new
`ResolvedExecutionPlan`, pins the MCP source snapshot and injects it. Tool names exposed to the LLM within one plan must
be unique; on a conflict, refuse to resolve or use a stable alias configured beforehand — never guess automatically.

The prompt tells the LLM whether each server is local STDIO or shared HTTP, its lifecycle and failure semantics, but never
exposes credentials or sensitive endpoints. A STDIO start-up failure and an HTTP network failure must be told apart; when
an outside result is uncertain, reconcile before retrying.

## Verification

The implementation verifies at least import, initialize, tools/list, a normal call, a parameter error, a permission denial,
a timeout and a bounded shutdown. An HTTP service update first passes health and tool-catalogue checks on a temporary port,
then switches or restarts; on failure the old service stays. The review checks that the manifest, the actual tools/list
and the agent's enabled tools match exactly.
