# Unified design v1.2 implementation

Current scope amendment: [C26](V12_C26_SCHEDULING_DEFERRED.md), approved 2026-10-11, retires external scheduling dependencies and defers all business timers until after P6. Earlier scheduling/probe entries below are historical evidence, not current delivery gates.

Human authorization: the complete 2026-10-08 upgrade plan was approved in chat
`01a118f1-8f64-7953-bc1d-9fcb8f318d43`, followed by explicit instruction to implement.
The original design and approved plan remain immutable under
`H:/AI/assistant-data/design-reviews/`. Runtime evidence and phase state are under
`H:/AI/assistant-data/upgrades/kernel-v12-20261008/`.

## Current live status — 2026-10-10

P0, P1 and P2 passed. Production is **0.28.1/core5.v8**, application source commit
`b5e521f94db9aae0f1cb10c6dec8628ae6ce2e0c`. The live API, channels and both
worker queues were verified. The original waiting Task, four accepted document
references and all business-table digests were preserved through cutover.
Phase acceptance is recorded in `evidence/P1-acceptance.json` and `evidence/P2-acceptance.json` under the upgrade directory.
D08 (model catalog) and D09 (explicit-empty to null) were approved and implemented.

P2's foundation and readers are installed and active. Implemented parts include native
compact boundaries without extra summaries, shared file agreement resolution,
fixed template candidates, DB-accepted Agent package readers, actual MCP discovery,
Supervisor assembly, accepted public background, directory disclosure and a
transactional session-boundary primitive. P2 acceptance covers 25/25 mandatory
conditions, 23 installed tests and real CLI/MCP/compact/resume/fresh evidence.
P3 is deployed and undergoing live acceptance. Typed Turn/report settlement,
concrete Prompt binding, stable canonical questions, final-baseline Review gates,
same-Task publication continuation, seven-day intent handling, transactional
accepted-document projections and Map 2.1 recovery now have isolated evidence.
The validator also runs in an owned process session with actual timeout/cancellation
and descendant cleanup tests. Separate new-work proposals preserve the
current Task until exact scope/Prompt confirmation; complete document and next
Prompt can share a confirmation with separate receipts. Complete literal original
instructions bind their actual source, without a manufactured user event. The dedicated
`kernel-v12-validation` Project alone has adopted the new method package. Existing
Kernel/trading business Projects retain their prior packages. Installed tests and an
isolated complete applicable flow passed; live Discord closure remains required.
C16 (lightweight criterion feedback), C17 (fresh recorded Supervisor sessions), and
C18 (configurable Supervisor timeout ceiling) are approved and implemented in this
candidate. C18 changes only the three tier ceilings to 900; defaults and production
effective values remain unchanged. Prior contract readers are preserved.

Actual isolated evidence now covers native child Skill generation, explicit parent
adoption, Kernel preparation awaiting confirmation, and a real PM/MCP call after
synthetic test confirmation. The PM produced a typed requirements document and Turn,
and settlement queued the next Supervisor. Subsequent actual model execution also
produced Design after synthetic combined Requirements/Prompt confirmation; its
completed output was settled without replay after correcting reference comparison.
Build has now returned a real inline deliverable and passed Kernel structure validation;
its already-completed result was settled without another model/tool execution. A real
cold Audit invocation has also been admitted against the final criteria. After the
approved C20 correction that same Task completed. This isolated proof does not replace
the live channel journey now in progress. P4-P6 have not yet formally started.

### Latest user direction: simplify the business mainline

The user explicitly prioritised a usable mainline and authorised removing unnecessary
call-chain complexity. The candidate now feeds v1.2 Supervisor one captured current
context rather than also building the legacy snapshot, empty business panel, brief
and repeated handoff. Current user sources, phase state, pending document body and
approved business baselines remain available. Old confirmed execution packages stay
referenceable but are not replayed as fresh instructions. Legacy Tasks retain their
existing reader and execution path.

Format repair receives only the failed response, exact error and active output schema.
It no longer repeats the business context or Prompt-generation schema and explicitly
forbids new work/tools/children. The normal path remains: user input, Supervisor/Skill
assembly (native child when useful), necessary clarification or concrete confirmation,
PM execution, result/state settlement and next applicable phase. No extra model role,
workflow platform, state table or user approval step is added.

First finish this path through Build and independent Review, plus ordinary questions,
stop/cancel and existing waiting-data preservation. Bulk historical migration remains
P6 work; additional rare-fault scenarios and speculative recovery machinery do not
block first-mainline work. Existing uncertainty holds and duplicate-execution checks
remain. This is sequencing and removal of duplicate plumbing, not a claim that all
v1.2 requirements have passed or permission to fabricate missing execution evidence.

After the concrete C19 question, the user affirmed continuation. The existing disposable
test Project now has explicit Supervisor limits of 500000 input / 100000 output, retaining
all prior usage and production defaults. The existing recovery-card controller resumed
the same Task; no counter reset or replacement Project was used.

Actual Build exposed two ordinary mainline defects, now repaired: inline text with no
file changes uses the exact archived PM result as its structure-validation source; each
PM Turn replies to its own concrete confirmation rather than repeatedly replying to the
Task's first message. Validation still does not establish business acceptance. Supervisor
context now includes the committed validation receipt/status and Review/publication
state, so completed validation need not be inferred or repeated. The model-facing
Supervisor schema also matches its existing empty approval_requests decoder rule.

C20 was approved by the user: the isolated Task explicitly requires only an
artifact, all mandatory acceptance criteria are artifact criteria, and the approved
Design says activation is not applicable. Its runtime_requirements also contains a
finite counting description. The former predicates treated that description alone
as mandatory runtime activation. The approved clarification uses explicit delivery
obligations, required runtime criteria and approved activation plans; genuine runtime
obligations cannot be waived. The same predicate now governs completion and handoff.
The same isolated Task completed through the existing controller with zero new
model calls and unchanged business documents; its final notice is queued in the
test Outbox. See `evidence/P3-C20-actual-task-completion.json`.

The 0.28.0 controlled cutover is verified: eleven services use its sealed environment,
66 installed tests passed, and protected business state remained equal. C20 passed 27 phase/feedback tests; the
updated fixed Role/Skill package passed 18 assembly/admission tests. Original
business Project adoption remains unchanged. P3 is not accepted until its required
live channel path is complete. The real App entry and clarification are verified.
An incomplete native child assembly timed out at 180 seconds; its failure and answers
were preserved. The delegated validation Project alone adopted normal timeout 600,
within approved C18, before a fresh consultation continued. Budgets and other Projects
were not changed. Proxy answers/confirmations are not human UI clicks.

Live evidence now covers real App ingress, accepted clarification, exact Prompt
delivery/confirmation, one actual PM requirements result, and the next combined
requirements/design display. The first Supervisor-created Task exposed missing
scheduling registration and repeated scope confirmation on subsequent Turns.
Installed 0.28.2 reuses the existing scheduling helper and limits scope creation
and its notice to unconfirmed work. Thirty-three affected tests passed. The live
test budget decision C21 is approved and adopted only for `kernel-v12-validation`
(Supervisor input 500,000/output 100,000); no counters are reset and P3 remains unaccepted.

Actual PM requirements and design outputs are archived. Design review found an
overbroad proxy attribution, and the legacy approval feedback branch attempted
to reuse a consumed PM confirmation. Candidate 0.28.3 routes pending inputs and
revision decisions to the original Task's Supervisor through the existing answer
controller; it introduces no new state, migration or recovery platform. Three
isolated feedback cases (direct revision, prior supplement plus revision, prior
supplement plus approval) preserve the document and confirmation, queue only
Supervisor, and create no correction Task. Live corrected design, delivery and
independent Review still remain to be verified.

Latest full candidate regression: 567 passed and three host-only tests skipped;
those three passed separately on Windows. The subsequent completion-blocker notice
repair passed 32 related tests. Audit admission now binds actual fixed input refs in
Kernel, keeps original model output and checks final requirements independently of
extra Prompt-local labels. Runtime-inapplicability annotations cannot waive required
artifact or runtime criteria. Invalid auxiliary user-intent updates are rejected and
recorded without creating authority or blocking a separately valid handoff.

| Phase | Current state | Remaining gate |
| --- | --- | --- |
| P3 | Candidate implementation and real isolated verification in progress | Finish mainline/common branches, review, controlled rollout and actual human acceptance |
| P4 | Formal implementation not started; earlier Python repair is reusable groundwork | P3 acceptance, then finite execution/resource and actual concurrency closure |
| P5 | Formal implementation not started; C26 removes the scheduling work package from this upgrade | P4 acceptance, then service/manual-maintenance closure; see V12_C26_SCHEDULING_DEFERRED.md |
| P6 | Formal implementation not started | P1-P5 acceptance, migration/recovery and final requirements review |

No new parallel control platform is planned. Reuse the existing messages, task queue,
configuration bindings, native execution, Outbox and recovery primitives; prioritize
the mainline and remove duplicate bindings before adding mechanisms.
See `V12_P2_ACCEPTANCE.md` and `evidence/P3-implementation-checkpoint.json`.

Real CLI evidence confirms two separate processes resumed the same native session,
retained the marker and produced a native `compacted` trace item. A separate actual
CLI call completed `get_authorized_workspace`. The installed CLI also rendered four
available agent slots (primary plus three children) for the explicit child limit.
These are technical integration proofs, not a completed PM business workflow.

P2 checkpoint (2026-10-08, 18:56 Asia/Shanghai): the second full regression run
passed 464 tests with three host-Git checks skipped; the three checks separately
passed on Windows. Subsequent context assembly and limited-grant fixes passed
their focused suites (3 and 5 tests). Real CLI evidence additionally verifies
resume retains an isolated marker and a fresh native session has no such value.
No production business-state differences were found in the preservation check.

The actual invocation now has a current file-agreement snapshot, fixed Agent/MCP
assets, explicit resolved configuration and a v1.2 context manifest. Supervisor
tiers/budgets are pinned per control Run; actual MCP discovery precedes both PM
and Supervisor model launch. A legacy gated/mcp_only Grant does not qualify as
an existing full agreement when processing a specifically approved conversion.

The user subsequently confirmed simple handling (D10): defer the additional
runtime directory-growth notification branch, retain bounded proposal measurement
and coverage disclosure, and impose no directory/Run size limit. No growth watcher
or deduplication ledger is introduced. This explicitly supersedes W08's growth
notification acceptance item for this upgrade; the original v1.2 remains intact.
The phase passed on 2026-10-09. `V12_P2_RELEASE_GATE.md` records the exercised
deployment/recovery procedure and the P3 handoff boundary.

Pre-cutover dependency review found that directory authority was incorrectly
gated on new business-output adoption. The prepared 0.27.0 environment is retained
as a rejected candidate and will not be deployed. In 0.27.1 every Project
invocation, including legacy PM/Supervisor, uses the shared directory agreement;
historical limited roots retain their access/write-mode and explicit label.
Candidate submission and business approval remain separate. Existing immutable
plans receive a new invocation scope binding without changing their business
package. No P2 production migration occurred before this correction.

Final verification found an inherited missing-compatibility-hash auto-fresh branch.
0.27.2 removes that trigger; known Role/config changes still create a fresh session.
The first activation preflight also exposed four unchanged CRLF-only source files;
they were backed up and normalized, preserving semantic content. AUTH01 then
authorized refreshing only three expired same-account auth.json files. Other
HOME/config/history and all business task records remained unchanged. Fresh login
backups are retained; expired token snapshots are not rollback targets.

The older notes below describe the P1 preparation history before its cutover.

## Baselines and phase gates

- Runtime-matching package source snapshot: `0598348de8f17af897009e8fc77c1cb8c4069092`.
- Existing Python repair snapshot: `69f52315e047a45a1e86ea20fa720afca58773da`.
- Original working tree and its 94 pre-existing status entries remain untouched.
- P0 passed: six PostgreSQL tables restored identically; pending Task and document
  refs preserved; installed consumer reads restored Vault; n8n snapshot integrity
  and seven existing Workflow states preserved.
- Isolated n8n 2.34.6 verified four real Workflow paths, in-flight planned due and
  workflow version metadata, explicit old-version activation despite a newer draft,
  and deactivation. The receiver is a test sink, not a completed Kernel executor.
- Isolated supervisord verified include/start/probe/stop/update, group cleanup and
  no automatic restarts. Unix sockets belong on Linux storage. A supervisord child
  has pgid=pid but an inherited sid; service cleanup must not assume sid=pid.
- Existing Python repair targeted tests: 37 passed in 43.59 seconds, isolated DBs
  and real test processes, no production deployment.

At this historical preparation point, P1 was in progress and P2 through P6 had not started. No stage may claim runtime
acceptance from a Schema check or test count. Production at that point was 0.25.0/core5.v7.

## P1 implementation and remaining gate

Implemented in this isolated checkout: fixed contract registry, scope-aware v3
documents with preserved historical readers, strict global configuration service
and bounded file watcher, filtered MCP configuration/document tools, transactional
Outbox helper, project change ledger and guarded v7/v8 migration. Existing Task
writers remain on their old adopted protocol. Configuration acceptance is separate
from consumer application; only the watcher can currently certify its own group.

Release preparation verifies source bytes, package bytes and dependency closure.
Runtime dependencies are constrained to the clean 0.25.0 environment; the original
backup's ambient-PYTHONPATH pip freeze is retained as historical evidence, not used
as the release dependency authority. The new inactive 0.26.0 environment passes
pip check and matches all old dependency versions. It has not been sealed or made
current.

Read-only upgrade inspection covers all business rows, pending questions and
accepted document references. Narrow pre-exposure rollback is tested: it refuses
new inputs, new ledger facts, unresolved effects or accepted new configuration.
After exposure, preserve new facts and use compatible readers/forward repair;
never restore a stale DB snapshot over new user input.

Verification records (all under the upgrade evidence directory):

- P1-full-regression.log: 438 tests before later compatibility additions.
- P1-mcp-wire-tests.log: actual stdio MCP with isolated PostgreSQL, strict DTOs,
  filtered ordinary-Project reads, hash checks and denied cross-Project/write paths.
- P1-rollback-tests.log: five transaction/rollback tests including new-input refusal.
- P1-reader-compatibility-tests.log: 21 passed, including an actual previous-library
  document read after replacing the installed library, and tamper rejection.
- P1-final-full-regression.log: 444 passed, three host-Git tests skipped, one
  subprocess setup failure because the candidate package was not yet installed.
  After installation, P1-installed-targeted-tests.log records 36 passed including
  that exact real MCP subprocess lifecycle. Host Git tests passed separately.
- P1-final-contract-tests.log: 17 passed after canonical LF generation and active
  public/internal DTO classification. P1-readiness-tests.log verifies real isolated
  PG schema admission, required ledger presence and tolerance of unrelated tables.
- python-repair-standalone-build.json: independent 0.25.1/core5.v7 wheel from commit
  `90f5ebdf329ae08b73230189178ca82732b32efe`, with only version metadata added to the
  preserved repair. All 103 package files match source; 17 installed-wheel tests
  pass. This package has not been deployed.

The configuration import candidate and per-leaf provenance are saved outside live
configuration. Import was held until explicit human decisions (now approved and applied):

- D08: existing `providers.*.chat_models` is absent from the approved SystemConfig
  schema; preserve the actual model/API fields through a minimal schema extension,
  or keep the old independent model catalog until a different decision is made.
- D09: old explicitly empty model names/endpoints must keep their unconfigured
  semantics. A null conversion or accepting empty strings requires confirmation;
  never replace them with working-looking default model names.

At that preparation point the P1 production gate had not passed. Configuration import, Kernel management tool
adoption, final release sealing, controlled deployment and post-cutover evidence
remain required before P2.
