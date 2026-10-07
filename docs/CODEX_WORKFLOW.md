# Usage-Efficient Autonomous Development Mode

Follow [Brian's authoritative project boundaries](../AGENTS.md). This is his
private, single-user LEGO hobby sorter. Preserve ordinary mechanical protections
and perform only absolutely necessary, change-scoped validation. ChatGPT provides
prompt generation and output oversight; Codex implements only the authorized
scope. This workflow does not authorize runtime or core-sorting changes.

## 1. Repository as memory

Start with [AGENTS.md](../AGENTS.md), applicable scoped instructions,
[CURRENT_STATE.md](ai/CURRENT_STATE.md), then the referenced approved slice and
its evidence. Read `AGENTS.local.md` when present for private local pointers.
Inspect documented answers before asking the user to restate them.

Code, tests, architecture documentation, approved ExecPlans and recorded state
outrank conversational recollection. Current explicit user instructions govern
scope and authorization, including corrections to earlier project procedures.
When documentation and exact artifacts disagree, record the discrepancy
and reconcile it without fabricating a state or silently discarding work.
An old approval or historical paused snapshot is not current operator readiness.
Dated state entries, reports and archived procedures remain historical evidence,
not current recovery policy or additional testing gates. Preserve them unchanged.

This candidate has no existing architecture document, ExecPlan convention,
testing manual or CI acceptance definition. Do not invent one retrospectively.
Use the approved task's existing plan/artifact location and link it from current
state. Keep a plan only as detailed as the slice needs: scope, approved behavior,
interfaces, affected checks and gates. Do not create parallel plan registries.

Existing implementation boundaries to inspect, not a mandate to refactor:

| Area | Source of truth |
| --- | --- |
| Coordination and subsystem communication | `software/sorter/backend/coordinator.py`, `subsystems/shared_variables.py`, `subsystems/bus.py` |
| Feeder motion and completion ownership | `software/sorter/backend/subsystems/feeder/go_to_angle/flow.py`, `hardware/sorter_interface.py` |
| C4 reservation, intake and indexing | `software/sorter/backend/subsystems/classification_channel/pocket_ledger.py`, `indexed_pocket_pipeline.py` |
| Distribution and accounting | `software/sorter/backend/subsystems/distribution/`, `runtime_stats.py`, `piece_records.py` |
| Persisted Harvest behavior | `software/sorter/backend/project_harvest_runtime.py` and affected Harvest tests |
| Frontend conventions | [CLAUDE.md](../software/sorter/frontend/CLAUDE.md), existing `/styleguide` implementation |

Paths abbreviated within a table row are relative to that row's subsystem or
backend root. Keep stable decisions beside the relevant code/plan. Update current
state at accepted boundaries with evidence pointers, not a debugging diary.

## 2. Autonomous same-scope development

After approval, implement through routine defects without requesting permission
for every correction. This includes argument/signature, timestamp/unit, type/lint,
fixture/mock, race/ordering, event/accounting and narrow integration errors;
documentation corrections and small necessary refactoring are also routine
within the authorized scope. Existing core sorting functions are an exception:
identify any necessary core change and obtain Brian's explicit approval before
implementing it, even when it appears to be a routine fix or refactoring.
An invalid test assumption may be corrected with evidence, not merely because
the implementation fails it. Never weaken assertions or suppress failures.

Proceed while architecture, intended behavior, hardware safety, external
permissions, privacy/security, provider configuration and scope remain within
approval. Diagnose and fix the root cause. If only a workaround is feasible,
explain the constraint/tradeoffs and obtain approval before using it. Install
missing required tools under the approved local setup scope; consequential
dependency/platform changes still need their applicable gate.

## 3. Consequential approval gates

Stop at an actual change in product behavior outside the slice, architecture,
subsystem scope, hardware safety/control philosophy, global motion settings,
firmware, production deployment/operation, destructive data behavior, unapproved
persisted production migration, privacy/security, substantial dependencies,
provider/account/credentials/billing, or a new paid service. Hive changes are
outside this project's authority; do not turn a local fix into a Hive dependency.

Existing core sorting functions always require Brian's explicit approval for
the necessary change. The reject-and-continue policy is a project requirement,
not blanket implementation authority. Preserve the normal **Upgrade** button
path and ensure all local add-ons continue functioning as originally intended
after upgrades. Upgrade compatibility is part of the authorized change's design;
these instructions do not authorize an upgrade, deployment or machine operation.

Inspect closely related same-scope issues before presenting a blocker so the
user receives one concrete checkpoint. Prepare all authorized local work first.
State the precise gate and its source; reuse approval already given for that
scope. Ordinary test failures, fixture fixes and small coding mistakes are not
approval gates. Safe pause/resume of long operations can be a real checkpoint.

## 4. Testing efficiency without lowering quality

Before work, specify the minimum absolutely necessary validation, applicable
accepted evidence to reuse, and a concrete stop condition. Implement only the
authorized change and run only checks needed to resolve its affected behavior or
findings. Integration, acceptance and independent review are scope-dependent,
not an automatic sequence of gates. Stop validation once the declared checks pass
and relevant findings are resolved; report remaining limits without expanding
qualification or creating unnecessary test infrastructure.

Reuse passing evidence only while its code, interfaces, environment and
assumptions remain valid. Record what ran anew versus what was reused. Rerun for
relevant edits, prior failures, review findings, changed interface assumptions or
invalidated acceptance. Do not repeat broad expensive suites merely for ritual.

Never convert failures to skips, weaken assertions, hide type/security failures,
or mock away the real contract under test. Separate software, simulated/local
integration, and live physical qualification. A successful command or counter
is not proof of physical clearance, delivery or useful throughput.

## 5. Project test tiers and actual commands

Run backend commands from `software/sorter/backend`. Its
[pyproject.toml](../software/sorter/backend/pyproject.toml) requires Python
**3.12**, with pytest in the dev group. Use the established environment and lock
file (`uv sync --frozen --python 3.12` only when setup is needed), then
`uv run --frozen python -m pytest ...`. Tests run against local temporary stores
and fixtures, never production databases or machine connections. Inspect the
selected fixture contracts first; collection is not a passing test run. Results
from another Python version are supplementary, not the supported runtime's
acceptance evidence. Do not silently upgrade dependencies to get tests running.

These are existing test targets, not new umbrella suites. Select only absolutely
necessary targets affected by the slice; tier denotes scope/cost, not a required
sequence or a measured duration promise. Reuse applicable accepted evidence.

| Tier | Existing command targets / purpose |
| --- | --- |
| FAST / focused | `uv run --frozen python -m pytest tests/test_go_to_angle_release_config.py tests/test_eject_controller.py -q` — release configuration and motor-owner transitions |
| FAST / focused | `uv run --frozen python -m pytest tests/test_runtime_stats.py -q` — deterministic accounting |
| MEDIUM / integration | `uv run --frozen python -m pytest tests/test_bounded_transfer.py tests/test_indexed_pocket_pipeline.py tests/test_indexed_pause_lifecycle.py -q` — paired perception, feeder owner, real ledger, reject and pause boundaries |
| MEDIUM / integration | `uv run --frozen python -m pytest tests/test_distribution_sending.py tests/test_distribution_rehome.py -q` — distribution completion and recovery |
| MEDIUM / integration | `uv run --frozen python -m pytest tests/test_project_harvest_project_routes.py tests/test_project_harvest_distribution_runtime.py -q` — API/store/routing boundaries using isolated fixtures |
| EXPENSIVE / acceptance | `uv run --frozen python -m pytest tests -q` — full backend, only when absolutely necessary for a specific unresolved change-scoped concern; record why narrower checks or accepted evidence cannot resolve it |
| EXPENSIVE / frontend | From `software/sorter/frontend`: `npm.cmd run check`, `npm.cmd run lint`, `npm.cmd run build` — actual [package scripts](../software/sorter/frontend/package.json); use `npm` off Windows. Do not rebuild an unchanged frontend. |
| EXPENSIVE / physical | Bounded approved hardware/browser qualification from the active plan; no generic live-test command or authorization is implied. |

There is no repository-wide acceptance command in this checkout. Browser and
large-data/persistence checks must use the applicable slice's real fixtures and
instructions. Do not invent a test script or a benchmark to fill a tier.
For documentation-only work, inspect the documentation diff and run
`git diff --check`, then stop. Do not run application tests or qualification.

## 6. Hardware-control workflow

Qualify software locally, identify exact candidate inputs, retain rollback bytes
and define a small live scope before requesting deployment/physical approval.
Existing deployment evidence/procedure pointers belong in `AGENTS.local.md`;
historical deployment scripts are not automatic permission to execute them.

For an approved checkpoint: establish current runtime/ownership and operator
readiness, use supported drain/stop behavior, compare deployed baseline hashes,
back up exact dirty/untracked bytes, deploy the allowlisted payload, verify running
versions and health, then perform only the authorized physical checks. Never
clear ownership or operational records to make a restart convenient. Rollback
restores the appropriate code, not newer operational history over older backups.
Preserve prior failed qualifications and cohort boundaries.

### Inert process replacement acceptance

Hardware state, controller lifecycle telemetry, and API/process health are separate.
A fresh backend with no controller is expected to report runtime lifecycle
`initializing` and `is_running=false` while hardware remains `standby`. Runtime
lifecycle `ready` is emitted by `SorterController.stop()`; an earlier reset that
stopped an existing controller can leave that value behind. Neither label proves
physical readiness, ownership clearance, or healthy hardware. Hardware `ready`
is a different field, reached through supported recovery/homing.

Do not require literal runtime `ready` for inert startup or issue reset/controller
creation to obtain it. Require a healthy expected new process, completed standby
startup, hardware standby with no error/recovery, absent controller, empty active
ownership/handoffs/reservations/transactions, disabled feeders, compatible current
configuration and protected history, and fresh physical evidence. Fresh process-local
activity must be zero; compare historical completion counts in durable records.
For pre-stop standby after a documented controller retirement, runtime `ready` is
also compatible with those independently established invariants.

### Proportional deployment decisions

Preserve operator-selectable configuration, including the Sorter-side Hive model
selection. Record differences; block only a demonstrated incompatibility with the
payload or a concrete operational risk. Unrelated configuration, tuning, historical
PIDs and isolated past restarts are not baseline failures. Verify the allowlisted
source bytes to avoid overwriting work; reconcile relevant differences locally.

Use the corrected wrapper in `AGENTS.local.md`: observe startup for up to 120 seconds,
with three consecutive healthy API/telemetry samples two seconds apart. Connection
refusal, incomplete telemetry and stale supervisor health during startup are retried.
An isolated restart requalifies the new process; repeated crashes/restarts or persistent
unreadiness after the grace period establish failure. Never roll back on one failed
probe or a generic exception. Do not repeat a start/stop command with an uncertain
response; reconcile its receipt and current process first.

Continue through recoverable/benign conditions within the authorized scope. Stop
agent deployment/operation work for unexpected motion, unresolved actuator-command
ownership, a genuine mechanical fault, meaningful source/data corruption, unapproved
destructive changes, inability to recover, or repeated crashes. Those agent work
boundaries do not impose human acknowledgment for ordinary uncertain pieces:
identification/tracking ambiguity or missing receiving evidence alone follows the
default reject-and-automatically-continue policy. Loss of observation calls for
bounded read retries and diagnosis, not assumed candidate failure or authority to
implement a core change.
Code rollback requires an established installation/startup failure and resolved stop
ownership; preserve current configuration and operational history. Hive is external
and read-only: model selection is not permission to administer or modify Hive.

This procedure supersedes blanket drift/uncertainty gates in old reports, scripts and
recollections. Historical Hive-administration prerequisites do not apply to Sorter OS.

An atomic installer must verify the exact noninteractive privileged interpreter
before shutdown, then validate every target's bytes/uid/gid/mode, required parent,
absence rules, filesystem flags and ability to create/chown/chmod/fsync/rename a
same-directory disposable probe. Prepare and verify all seven temporary payloads
before requesting a child stop. Keep the same privileged worker through the bounded
operation; recheck all gates before stop and all prepared/target metadata afterward.
A failed privilege/probe check must leave the backend running and originals untouched.
Never relax permissions. Replacement is atomic per file, with the child held stopped
until the entire allowlist verifies. Keep durable original bytes plus recorded absence
for newly introduced files; only the authorized software-fault rollback can restore
code. Database/configuration restoration is not implied by code rollback.

The default recovery policy is to reject uncertain pieces and automatically
continue. Identification/tracking ambiguity, uncertain per-piece custody or
missing receiving evidence alone must not require human acknowledgment. Human
intervention is for genuine mechanical faults. Preserve ordinary mechanical
protections and honest records; do not fabricate receiving evidence or deliberately
jam the machine. Do not silently tune thresholds, timing, motion or routing, or
implement this policy in existing core sorting functions without Brian's explicit
approval for the necessary change.

## 7. External providers

Keep provider/API availability and failure evidence separate from transport,
classification, routing and mechanical outcomes. Do not adjust accounts,
credentials, billing, paid plans or production provider configuration without
approval. Offline fixtures must preserve protocol and interface requirements;
they cannot establish live availability. Hive remains external/read-only for
this work; existing client usage documentation is not authority to send requests,
administer Hive or require a Hive-owner change.

## 8. Independent review and Git boundaries

Use independent review when necessary for the authorized change near a
commit/release boundary. Review the bounded diff, applicable evidence and risks;
fix findings within scope and the core-sorting approval boundary, and rerun only
necessary affected checks. Do not require broad acceptance or repeated review by
default. Self-review is not independent review; report any required review that
remains pending.

Inspect index and worktree before edits. Preserve unrelated dirty/untracked work;
do not reset/stash/clean or broadly stage. Compare candidates to exact deployed
bytes when relevant. Stage/commit only on instruction, with an explicit file
allowlist and reviewed diff. Readiness is not a commit or deployment.

## 9. Reporting efficiency

Default to files/functions changed, new/reused checks, important decisions,
limitations, implementation/deployment status and the next real gate. Use longer
forensics only for unusual production/hardware failure, ambiguous operational
data or evidence needed for root-cause analysis. Report failures honestly and
avoid lengthy reports for routine corrections.

## 10. Conversation lifecycle

Use one Codex/Astra conversation for one coherent implementation/review slice,
including fixes, tests and qualification preparation. At an accepted/committed
or otherwise stable boundary, update current state and prefer a fresh conversation
for the next slice. The repository is the memory boundary; do not require an old
debugging transcript to start work. Do not create a new conversation automatically.

## 11. Short invocation and checkpoints

> Implement the approved slice from [PLAN]. Follow all applicable AGENTS.md
> instructions, docs/CODEX_WORKFLOW.md, and docs/ai/CURRENT_STATE.md. Work
> autonomously through same-scope defects within Brian's explicit core-sorting
> approval boundary. State minimal validation and a stop condition; reuse accepted
> evidence and run only absolutely necessary change-scoped checks. Stop when those
> checks pass and findings are resolved, or at a true approval gate. Do not deploy
> or operate production unless this
> prompt explicitly authorizes it. Leave work uncommitted unless instructed otherwise.

Replace `[PLAN]` with the existing approved task/plan path. Use checkpoints for
the consequential gates in section 3 and unresolved blockers, not ordinary
corrections. Short invocations do not expand the referenced slice's permissions.
