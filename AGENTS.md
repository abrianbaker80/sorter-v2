# AGENTS.md

**This file is committed to the public repo.** Anything here must be generally
useful documentation for any agent or contributor working in this repository:
how things build, where things live, what the conventions are. Nothing
specific to one person's machines, accounts, or setup goes in this file; that
belongs in the gitignored `AGENTS.local.md`.

## Start here

- Read [current development state](docs/ai/CURRENT_STATE.md) before development
  and follow [the development workflow](docs/CODEX_WORKFLOW.md).
- Use repository code, tests, approved plans and evidence as durable memory;
  inspect them before asking the user to repeat history. Follow applicable
  scoped instructions and preserve the existing subsystem architecture.
- Complete approved same-scope work autonomously, including routine defects.
  Fix root causes; do not hide failures or substitute an unapproved workaround.
- Develop with focused affected tests; run applicable acceptance once stable
  and obtain independent review near meaningful commit/release boundaries.
  Reverify affected behavior after corrections, not every unrelated suite.
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
