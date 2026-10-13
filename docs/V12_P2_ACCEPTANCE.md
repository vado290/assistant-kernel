# P2 implementation and acceptance — 2026-10-09

**PASS for the approved P2 foundation/deployment gate: 25/25 mandatory conditions aligned.**
Production runs **0.27.2/core5.v8**, built from
`2e4dfae9dab6af6c0289ba8515790b55bb2a1e86`. All 158 application files match
the sealed installed package. Eleven Kernel services use that environment with
no source PYTHONPATH; readiness confirms two interactive and four runtime workers.

The authority is the user-approved plan at
`H:/AI/assistant-data/design-reviews/kernel-v1.2-upgrade-plan-20261008/`,
manifest SHA `1d559da35d1d7d160a477be887b81b09f6a0f9a274a7a49999933db7805fe205`,
and the unchanged original v1.2 report SHA
`f9ae591846cc24c57687dd9bc35c8fa0f940668a83bcf1e66ff1f0414a4a3b29`.
Effective amendments include D07's exact Kernel directory approval, D10's
directory-growth simplification, and AUTH01's same-account credential refresh.

The atomic matrix and hashed evidence references are in
[P2-acceptance.json](H:/AI/assistant-data/upgrades/kernel-v12-20261008/evidence/P2-acceptance.json).

| Work package | Accepted P2 behavior and evidence | Later-phase boundary |
| --- | --- | --- |
| W07 shared directory agreement | Current DB grants reach the next invocation; Project roles share file capability; old limited roots remain limited; cancellation retains grants; missing mounts report capability gaps; formal business approvals remain separate. | No old Task or approval was advanced. |
| W08 directory proposals | Fixed proposal/answer binding, own/external recursive scope, bounded metadata and exact/lower-bound disclosure; no deletion or authority shrink. | D10 explicitly defers extra runtime growth notifications; advisory proposal measurements remain. |
| W09 native sessions | No occupancy/unknown-usage summary rotation; actual compact/resume/fresh traces; absent legacy compatibility evidence alone keeps continuation; genuine Role/config changes remain fresh; boundary primitive enforces CAS and validator participation. | The complete business stage controller connects SessionBoundaryService in P3. |
| W10 fixed assembly | Accepted configuration/package readers, actual required MCP discovery, per-invocation scope/config/context receipts, leased ephemeral Supervisor and a three-child native limit. | New result ABI adoption is atomic in P3; existing business output protocols remain compatible. |
| W11 public templates | Thirty-one normative Role/Skill assets, five profiles and fixed schema library published with immutable hashes; 248 rule rows map to fixed loading/prompt enforcement and their downstream mechanisms. | New public business packages and local customization reconciliation proceed through P3 adoption. |
| W12 public background | DB-accepted Project document, rebuildable display projection, selected subset hashes, purpose/field withdrawal, original dates and same-source facts preserved. | Service background refresh is exercised with managed maintenance in P5. |

Verification combines source/contract checks, real isolated PostgreSQL integration,
installed-package tests, actual Codex CLI/MCP execution, process identities and
production state readback. The final patch passed 23 installed tests and eight
focused compatibility/continuation checks. Earlier full regression had 466 passing
tests and one obsolete Audit permission assertion subsequently corrected and
verified; the three host-Git checks passed separately. Counts do not substitute
for the requirement matrix.

The real runtime proof completed an actual MCP call, retained the marker across
native resume/compact, and omitted it in a fresh native session. Its original
negative prompt contained its own marker prefix; the false exact-text assertion
was corrected with a separate new-session probe. The harness, scope resolver and
MCP implementation are byte-identical between that exercised 0.27.1 package and
the final 0.27.2 patch. The changed processor branch was tested in the installed
0.27.2 package. No complete P3 user business flow is claimed here.

Production messages, Runs, sessions, model-invocation records, the existing
waiting interaction and four accepted historical document references retain
their pre-cutover digests. Only the approved D07 Grant transition occurred;
its original row is retained. Ordinary configuration values and existing n8n
workflows were preserved. No waiting Task was answered, cancelled or consumed.

The earlier generic execution-policy rejection did not recur after renewed user
authorization. Activation's first preflight exposed four CRLF/LF-only source
differences; their unchanged semantic content was verified and backed up before
byte normalization. Real model verification then exposed expired credentials.
After AUTH01, only the template, Kernel and trading-risk-bot auth.json files were
refreshed from the existing same-account login. Their other HOME/config/session
files and task records were preserved. No recurring auth platform was added.

Recovery uses the P2 cutover PostgreSQL/files/config/n8n snapshot, restored patch
database, guarded source deltas, immutable source bundles, retained runtime
environments and the accepted fresh-login backup. Old expired tokens are historical
evidence, not a rollback target. Any rollback preserves subsequent input,
approval, result/effect and credential facts; compatible readers or a forward
repair precede execution if the old binary cannot consume them.

P3-P6 remain explicit obligations, including full Prompt confirmation, canonical
dual-entry questions and a real human path; complete phase/review closure; Map
2.1; business Python/graphs and shared capacity; managed services and n8n;
operations, external configuration application and Ubuntu recovery. The upgrade
as a whole is not declared complete by this P2 acceptance.
