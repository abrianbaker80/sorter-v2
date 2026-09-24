# C3-C4 steering audit — 2026-09-12

Local procedure correction only. No SSH, deployment, hardware operation or Hive
access was performed. Historical attempt reports remain evidence, not current gates.

## Instruction sources steering Astra

| Source | Finding |
| --- | --- |
| Current user instructions and app/system/developer instructions | Authorize this local correction, prohibit deployment, preserve Hive read-only. Current scope supersedes earlier procedural wording. |
| `C:/Users/abria/.codex/config.toml` | Astra/medium; memories enabled; no custom instruction file, inline instructions, fallback filename, or project-doc size setting. Browser/Chrome tool instructions are unrelated. |
| `C:/Users/abria/.codex/AGENTS.md` | Install missing required tools; fix root causes, ask only if a workaround is necessary. No drift/readiness veto. Global `AGENTS.override.md` absent. CODEX_HOME shell variable unset; default home inspected. |
| Workspace `.codex/config.toml` | `approval_policy=never`, `sandbox_mode=danger-full-access`; no instruction indirection. No additional ancestor or candidate `.codex/config.toml` found. |
| Ancestor instruction search | No AGENTS.md/override in C:/, C:/Users, C:/Users/abria, C:/Users/abria/Downloads, or original workspace root. Added a short workspace AGENTS.md routing this candidate. |
| Candidate `AGENTS.md` | Explicitly imports workflow, current state and `AGENTS.local.md`. Added proportional configuration/readiness rule. |
| Candidate `AGENTS.local.md` | Private evidence catalog, loaded by explicit reference, not a configured fallback. Now points to corrected wrapper and retires older attempt procedures. |
| Referenced user attachments `d82cfaec-4f1c-44c3-9dad-2be673ea063f/pasted-text.txt` and `f1d80523-35a4-422f-9d93-423f0ac6bf91/pasted-text.txt` under Codex attachments | Explicitly say cardinality uncertainty should not stop the sorter, distinguish recoverable handling from ownership ambiguity, and preserve Hive read-only. They do not require blanket drift or single-probe rollback. Their earlier task scopes are historical. |
| Scoped/fallback files | No candidate AGENTS.override.md or additional scoped AGENTS.md; referenced docs/electronics instructions are absent in this checkout. Frontend CLAUDE.md is conditional UI guidance, not applicable to this change. Sibling checkout instructions do not govern this candidate. Workspace `.agents` has no files. |
| Memory injected by the app; `C:/Users/abria/.codex/memories/MEMORY.md` | Historical catalog advice at lines 840-851 includes Hive administrative-access hard blockers. Stale and incompatible with current Sorter-only scope; not edited. Workflow explicitly rejects carrying those prerequisites forward. |
| OpenAI Docs skill | Used only to verify instruction discovery; no deployment stop/rollback rule. Other available skills are not implicitly loaded deployment policies. |

Discovery order was checked against [official AGENTS.md documentation](https://learn.chatgpt.com/docs/agent-configuration/agents-md).
This inventories locally applicable sources, not instructions on an uncontacted remote host.
The working directory is a workspace container, not a valid Git checkout; the
actual candidate checkout is `sorting-flow-candidate-20260911`.

## Exact causes: documentation versus executable logic

- **Documentation steering:** workflow section 6 required “unchanged qualified
  configuration”; section 1 let “stronger applicable project safety rules” survive
  a current user correction. Current state retained contradictory old readiness
  gates and an unreusable-wrapper warning. Rewritten with proportional decisions.
- **First model-selection refusal:**
  `analysis_artifacts/c3-c4-fresh-deployment-20260912/deployment-report.md` records
  only `detection.carousel.algorithm` changing and the configuration gate stopping
  progress. That report says the unused procedure drafts were deleted; their exact
  implementation cannot be recovered from these retained files.
- **Retained executable equivalents:** old `c3-c4-deployment-20260912/remote_deployment.py`
  requires literal lifecycle ready and EXPECTED_CONFIG equality; activation
  `worker.py` checks full configuration/tuning/frontend/source equality; handover
  `worker.py` pins a configuration hash. These are now explicitly retired evidence,
  not sources for the next deployment procedure. Their historical bytes are retained.
- **Current executable configuration veto:** fresh-attempt `resume_remote.py`
  `configuration()` allowed only the selected model to differ from an old TOML;
  deploy also asserted full config equality between stages and full tuning equality.
  Removed these vetoes; valid TOML/model/hash are recorded, actual feeder/ownership
  invariants remain enforced. No configuration is written.
- **Actual premature rollback:** `resume_remote.py::start_once()` trusted cached
  `backend_healthy`, then made an uncaught immediate `/runtime-stats` request.
  Its surrounding `except Exception` rolled back. `resume-evidence/resume-failure.json`
  records connection refusal, while `resume-report.md` records successful standby
  startup in 2978 ms. Fixed the caller with real API/telemetry observation, not a
  sleep or a runtime lifecycle change.
- **Supervisor contribution, not rollback logic:** backend `supervisor.py::status()`
  computes health from a prior success timestamp; `_start_backend()` retains that
  timestamp. Its health loop records probe failures, not deployment rollback.
  Therefore the wrapper must verify the new API directly; supervisor runtime was
  not changed and its cached health alone is no longer sufficient.
- **Other executable checks reviewed:** existing `atomic_install.py` retains
  source/metadata integrity and recoverable original bytes; no config gate or
  startup rollback is present there. `standby_acceptance.py` rejected even one
  historical fast crash; it now rejects repeated crashes (two or more).
- The referenced bounded-transfer, C3 recovery and distribution READY qualification
  reports describe earlier authorization/physical-acceptance boundaries; those
  historical NO-GO labels do not veto this correction or a newly authorized attempt.
  The older bounded-transfer `live-checkpoint/deploy_remote.py` also has a broad
  startup-exception rollback; it is covered by the explicit historical-script retirement.

## Changed files and behavior

Paths below are relative to the workspace container:

- `AGENTS.md` — new short candidate routing instruction.
- `sorting-flow-candidate-20260911/AGENTS.md` — proportional rule.
- `sorting-flow-candidate-20260911/AGENTS.local.md` — corrected entrypoint, fresh
  attempt naming, explicit retirement of historical wrappers.
- `sorting-flow-candidate-20260911/docs/CODEX_WORKFLOW.md` — current user precedence,
  relevant compatibility only, bounded retries and established-failure rollback.
- `sorting-flow-candidate-20260911/docs/ai/CURRENT_STATE.md` — concise, consistent
  state; historical live observations clearly distinguished from this local audit.
- `sorting-flow-candidate-20260911/docs/ai/DEPLOYMENT_STEERING_AUDIT.md` — this inventory.
- `sorting-flow-candidate-20260911/scripts/deployment_readiness.py` — 120-second
  wall-clock bound, three healthy samples two seconds apart, isolated restart
  requalification, repeated-failure classification, no process mutations.
- `sorting-flow-candidate-20260911/scripts/test_deployment_readiness.py` — offline
  regression tests with simulated clock/HTTP/processes and actual wrapper branches.
- `analysis_artifacts/c3-c4-fresh-deployment-20260912/resume_remote.py` — uses that
  observer, retries read transport errors, does not replay uncertain start requests,
  no generic-exception startup rollback, requires confirmed stop before restoration.
- `analysis_artifacts/c3-c4-fresh-deployment-20260912/resume.py` — composes corrected
  helper; unique attempt directory required; outer budget includes candidate and
  possible original startup grace periods. Private evidence must travel with it.
- `analysis_artifacts/c3-c4-deployment-20260912/blocker-resolution-20260912/standby_acceptance.py`
  — permits a single historical crash.
- `analysis_artifacts/c3-c4-deployment-20260912/blocker-resolution-20260912/atomic_install.py`
  — comment-only removal of a stale configuration-drift prerequisite; executable
  source/metadata checks and rollback implementation are unchanged.

Rollback still requires resolved command ownership. Observation loss or unsafe
activity does not authorize blind stop/restoration. No history/configuration restore
or Hive administration is introduced. Source/hash incompatibilities are reconciled
before replacement rather than overwritten.

## Validation and disposition

18 focused unittest tests passed under Python 3.12, including connection refusal,
incomplete telemetry, bounded deadlines, isolated/repeated restarts, lost command
response, configuration changes, and executing the wrapper rollback branches with
fake operations. Composed worker compiles without executing Linux/process code.
Changed-file syntax/whitespace and document links passed; the candidate ZIP hash
was verified unchanged. Existing unrelated whitespace findings were left untouched.
No 211-test candidate qualification rerun; no product runtime or archive changes.

Local steering/procedure is ready for a clean, separately authorized attempt with
fresh live checks. This audit does not claim current live readiness or physical
qualification. Global memory still contains the stale Hive prerequisite; current
instructions and the corrected workflow supersede it. No global instruction or
memory files were edited. Existing dirty work remains uncommitted.
