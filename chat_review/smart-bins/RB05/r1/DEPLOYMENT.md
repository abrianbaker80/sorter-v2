# RB05 later deployment and rollback — plan only

RB05 performs no deployment, machine access, schema initialization, firmware action or physical test. A separate authorization and operator runbook review are required before using this plan.

## Identities and preservation

- Candidate source result tree: `c8c43b0261c1d581575fcd65785140b416dc6d44`, reconstructed from pristine v0.3.0 tree `21e805b60da575ff7468fcc3570a517d80f01cd0` through accepted RB01–RB04 and `RB05.patch`. The implementation branch remains uncommitted; materialize an immutable deployable revision from this exact tree only in a separately authorized release step.
- Supplied live software baseline to preserve and verify on the actual machine: `sorter/stable/v0.2.9`, commit `2c8269843c16282659c9edbfbf03e7c7042a15ce`.
- Supplied live firmware baseline to preserve and verify: `firmware/v0.8.1`, commit prefix `8d560d26`, variant `distribution-v1-2`. Firmware remains at v0.8.1 unless separately authorized. Do not flash during this qualification.
- Preserve the existing `machine.toml` and tuning unchanged for the source comparison unless a concrete incompatibility is found and separately reviewed. Inspect and resolve any conflicting `classification_channel`/`carousel` camera definitions before deployment; canonical `classification_channel` currently wins.

## Before source update

Record exact installed checkout, service identity, firmware self-report/variant, configuration hash, schema/version inventory and physical state. Back up the complete source revision and service launch definition, `machine.toml` and calibration, and a consistent SQLite snapshot including WAL contents. Verify backup readability and a reversible source/config/database restoration path. Record current Smart Bin claims, holds, discrepancies and current obligations. Activation requires zero unresolved obligations or an explicitly accepted reconciliation. Do not overwrite any active/UNCERTAIN claim with a rollback snapshot.

## Ordered later procedure

1. Deploy and identify the clean v0.3.0 source first in **ordinary Smart-Bin-absent mode**. Confirm normal startup does not create or require Smart Bin or Harvest schema. Perform Stage 1 of `QUALIFICATION.md`: homing, servos, C3/C4, route/landing, multi-drop bucket safety, stall auto-clear and manual control. Preserve the observed ordinary result as the same-machine comparison baseline.
2. If the ordinary smoke fails, stop and rollback the source/config/database set without Smart Bin activation. Diagnose the exact incompatibility separately; do not tune around an unexplained failure.
3. Update the source to the exact RB05 result tree, still with Smart Bins absent. Verify the installed tree hash and repeat the ordinary native smoke at a scope sufficient to rule out an RB01–RB05 regression before activation.
4. Only after ordinary acceptance and separate activation authorization, initialize RB02 core storage schema and service schema explicitly on the reviewed local SQLite database. Prepare approved machine, policy, group, session and slot/opening-balance facts. Verify version/readback and all prerequisites; never rely on import/startup to migrate.
5. Explicitly initialize RB03 native custody with approved machine/policy/namespace/routing revision, then RB04 native completion extension and its local prerequisites. Verify every extension version, required object/index/trigger, current-obligation projection and zero stale claims. Any partial/mismatched schema fails closed; stop rather than auto-repair on startup.
6. Execute Stage 3 dry guards with no fabricated receiver evidence. A missing receiver must hold without credit or a second release. Do not permit continuous Smart Bin sorting before the separately accepted Stage 4 receiving-evidence source/policy and guarded Stage 5 lot. No external Harvest confirmation or activation belongs to this deployment.
7. With all physical gates separately accepted, run Stage 6 paired sustained comparison and accept performance/quality using the user's preapproved gate. Keep exact source, firmware, config and lot identities in the record.

## Rollback

Trigger rollback on any `QUALIFICATION.md` safety halt, schema corruption/mismatch, failed ordinary smoke, unexplained custody state, or rejected quality/performance result. Stop admission and motion with the operator; preserve logs, profiler aggregates, current SQLite state and physical observations before changing software. Restore the saved source and service definition, unchanged configuration and a mutually consistent database snapshot only after durable claims are reconciled under explicit authority. Verify installed hashes and ordinary native behavior after restoration. If physical custody is uncertain, leave motion blocked and obtain a reviewed recovery plan; a source rollback alone cannot clear a physical claim. Firmware is not changed in this procedure.

## Exclusions and gate

No Harvest application/store/provider/network integration is activated. P2C6B remains blocked until RB05 source acceptance, separate deployment authorization, ordinary v0.3.0 physical smoke, receiving-evidence policy qualification, guarded Smart Bin physical qualification, and performance/continuous-operation acceptance. Review `QUALIFICATION.md` before any physical authorization.
