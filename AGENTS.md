# AGENTS.md

This file records Brian's authoritative project boundaries and contributor
instructions for this checkout. Private machine, account, credential and access
details belong in the gitignored `AGENTS.local.md`, never in committed files.

## Brian's authoritative project boundaries

1. This is Brian's private, single-user LEGO hobby sorter, not a safety-critical
   or perfect per-piece-accountability system. The default recovery policy is to
   reject uncertain pieces and automatically continue. Identification/tracking
   ambiguity or missing receiving evidence alone must not require human
   acknowledgment. Human intervention is for genuine mechanical faults.
   Preserve ordinary mechanical protections.
2. Do not change existing core sorting functions without Brian's explicit
   approval. These boundaries are not blanket authorization for runtime changes.
   Identify any necessary core change and obtain approval before implementing it.
3. Preserve upgrades through the normal **Upgrade** button. All local add-ons
   must continue functioning as originally intended after upgrades.
4. Perform only absolutely necessary, change-scoped testing. Reuse accepted
   evidence while it remains applicable, specify minimal validation and a stop
   condition, and avoid unnecessary test infrastructure or repeated broad
   qualification.
5. ChatGPT provides prompt generation and output oversight. Codex implements
   only the authorized scope.

These boundaries supersede contradictory active instructions and older project
assumptions. Dated state snapshots, reports and archived procedures remain
historical evidence; do not rewrite them or treat their old gates as current
policy. Recording these boundaries does not implement the recovery policy or
authorize application, configuration, schema, firmware, deployment or machine
changes. Existing core behavior remains unchanged until explicitly approved.

## Start here

- Read [current development state](docs/ai/CURRENT_STATE.md) for recorded evidence
  before development and follow [the development workflow](docs/CODEX_WORKFLOW.md)
  within Brian's authoritative boundaries above.
- Use repository code, tests, approved plans and evidence as durable memory;
  inspect them before asking the user to repeat history. Follow applicable
  scoped instructions and preserve the existing subsystem architecture.
- Complete approved same-scope work autonomously, including routine defects,
  within the explicit core-sorting approval boundary. Fix root causes; do not
  hide failures or substitute an unapproved workaround.
- Select the minimum necessary validation and its stop condition before work.
  Reuse applicable accepted evidence; rerun only checks affected by changes or
  unresolved findings. Acceptance and review must be proportionate to the scope.
- Preserve dirty/untracked work. Use explicit path allowlists; do not reset,
  clean, stash or broadly stage. Leave work uncommitted unless instructed.

## Non-negotiable boundaries

- Production deployment and live hardware operation require explicit scope
  approval. Never silently restart, home, drain, clear or operate the machine.
  Firmware and global motion/configuration changes are consequential changes.
- Preserve operational records, Harvest/bin/history and authoritative ownership.
  Destructive data changes and persisted production migrations need approval
  unless already expressly included in the approved slice.
- External provider/account/credential/billing changes require approval.
  Hive is external and read-only for project work: do not administer, deploy,
  restart or modify Hive, or require its owner to change it.
- Use exact deployed bytes when evidence identifies dirty/untracked production
  source; never reconstruct that baseline from Git HEAD. Follow the approved
  hash verification, backup, deployment and rollback procedure.
- Preserve operator-selected configuration, including Hive model selection;
  unrelated drift is not a blocker. Use bounded readiness retries and require
  established failure before rollback. Apply the proportional procedure in
  `docs/CODEX_WORKFLOW.md`, not historical blanket STOP/NO-GO language.
- Usage efficiency never permits weaker tests, hidden failures, reduced
  security, unsafe hardware behavior or skipped review/deployment safeguards.

## Scoped instructions

Preserve these scopes when present in the working revision (the two paths below
are referenced by the inherited instructions but absent in this candidate):

- `electronics/wire_harness/AGENTS.md`: the wire harness, and the derived-asset
  pipeline (sources in git, renders in the assets bucket, CI publishes).
- `docs/AGENTS.md`: working on the docs site.

For sorter UI work, also read the existing
[frontend design rules](software/sorter/frontend/CLAUDE.md).

If a gitignored `AGENTS.local.md` exists next to this file, read it too: it
carries machine-local private context (machine names, access details) that
never gets committed.
