# P2C6P-C02 r1 — MCU response acquisition

## Result and scope

The reader now consumes available bytes in chunks, reassembles split transfers,
and returns through the first terminator. Candidate transaction ownership and
ambiguity handling remain intact. This is source/test qualification only.

**24 focused tests passed; 56 affected tests passed; Ruff passed on all three
files. Independent review approved with zero actionable findings.**

Implementation remains uncommitted on `sorting-flow-candidate`, HEAD `c4e298ebd1a153001355df3d3b0c41cc5446ac8f`,
with an empty, byte-identical index. Only these implementation paths changed:

- `software/sorter/backend/hardware/bus.py`
- `software/sorter/backend/tests/test_hardware_bus.py`
- `software/sorter/backend/tests/test_mcu_bus_read.py`

## Exact four-way source comparison

All source comparisons used Git objects locally, without accessing the live
machine. The v0.3.0 commit was fetched into the existing isolated upstream source
repository; candidate checkout refs and files were not replaced.

| Source | Exact revision/tree | hardware/bus.py blob |
| --- | --- | --- |
| candidate_c01 | `b604851c01253880d331525884d0dc280a478485` | `d9978846ae360c7b26bca098ab96d0895db8a8e2` |
| v0.2.9 | `2c8269843c16282659c9edbfbf03e7c7042a15ce` | `0f62ffdf8b1d96738ea8e90c606614612ac38379` |
| v0.3.0 | `8951f914eb34778139b0df42f59f3957abbc2f8a` | `0f62ffdf8b1d96738ea8e90c606614612ac38379` |
| upstream_fix | `94622df04d046f1de2191bba99887fc4cc67ff18` | `0f62ffdf8b1d96738ea8e90c606614612ac38379` |

**v0.3.0 did not change hardware/bus.py materially: its entire file is byte-identical
to v0.2.9 and upstream fix 94622df0.** All three have SHA-256
`cb85690ca36035f42d0d0812ad76ab997025064d30174dbf935b46b0792b3466`. Accepted C01 has SHA-256
`d864f4bd5987b62a90c76cc28cd71cf61d2de44907cbe54f6bfe6dfd18425d4c`. The analysis-only stop gate is not triggered.

The candidate reader used up to four `read_until` windows. The upstream reader
uses `in_waiting`, bounded chunk requests, per-read serial waiting, a frame size
of 254, and a one-second monotonic collection deadline. C02 adopts those reader
behaviors, retaining the candidate helper name and optional size argument and
capping that argument at 254. The deadline is checked before starting each read.

The upstream retry loop, per-attempt lock scope, backoff, response payload slicing,
and less strict validation are **not adopted**. Candidate validation and failure
bookkeeping stay inside its single bus lock. There is still exactly one write
site per `send_command` invocation and no resend loop.

Firmware has no transaction ID. A timeout does not prove nonexecution; a delayed
reply cannot identify a newer operation or requested motor. Retaining per-address
unresolved fencing and ignoring response channel for ownership therefore remains
necessary. Actual channel bytes remain visible in returned messages. This slice
does not assume replies arrive in one USB transfer, channels echo, or late replies
cannot arrive.

## Accepted chain and pre-fix reproduction

Reconstructed every accepted patch from candidate HEAD through P2C6A in a newly
created temporary Git index, verifying each patch SHA-256 and intermediate tree.
P2C6A reconstructed to `8b4d95a94d25d5c6931b57e210bf3e87de8b7e22`.
Applying accepted C01 in that index yielded `b604851c01253880d331525884d0dc280a478485` exactly.
All 41 paths changed by the accepted chain match the current checkout's canonical
Git bytes; the bus and existing bus test were checked against C01 as well.
No accepted patch was reapplied to the implementation checkout.

The reproduction loaded the exact bus blob extracted from that accepted C01 tree
under an isolated module name, with the same deterministic SerialBase helper now
included in the patch. A complete already-arrived 11-byte valid reply produced:

| Probe | `read_until` calls | Requested read sizes | Request writes | Result |
| --- | ---: | --- | ---: | --- |
| Accepted C01 | 1 | eleven requests of size 1 | 1 | Valid reply |
| C02 test | 0 | one request of size 11 | 1 | Valid reply |

This reproduces the inefficient acquisition shape, not a claimed business failure
or physical benchmark. Virtual per-call overhead of 15 ms with arrival at 2 ms is
also covered: C02 requests `[1, 10]`, returns the valid reply, and writes once.
No wall-clock speed threshold is used.

## Reader contract and limits

- Bytearray collection; consume available bytes up to remaining capacity.
- If none are waiting, request one byte and let the configured serial timeout wait.
- Search after every chunk and return through the first zero only. Trailing zeros
  and other bytes in that chunk are not saved for a later command. No persistent
  receive buffer or transaction protocol was introduced. Existing pre-write input
  reset remains; same-address fencing handles an ambiguous prior exchange.
- Stop at an empty read, capacity, or monotonic deadline; return partial data to
  the unchanged timeout/partial/max-size validation paths.
- No `read_until` remains anywhere in production bus.py.

`MAX_FRAME_SIZE = 254`; `MAX_FRAME_READ_S = 1.0`. As in upstream, a read already
started can finish under its configured serial timeout. Thus the one-second
deadline bounds starting further reads; collection can overrun it by the final
individual serial wait and scheduling overhead. The code does not change the
port timeout or promise a hard real-time wall-clock limit. Virtual-time tests
cover continuous arrivals across the deadline and prevent an extra read after a
slow chunk. A silent MCU still incurs only one serial timeout.

The old fake represented an empty transfer as an empty return from `read_until`.
It now models `in_waiting == 0` and `read(1)` waiting for the next transfer. The
existing successful gap test still verifies payload, three reads, and one write;
it additionally verifies the wait. A true empty `read()` after timeout terminates
collection, so the unfinished-frame test now expects two calls rather than four.
New virtual-time tests distinguish a 50 ms gap within a 100 ms timeout from a
150 ms late remainder, which fails and leaves the address fenced. No ownership
assertion was weakened, and all 32 existing hardware-bus test cases pass.

## Safety evidence

New tests explicitly assert **one write even with `retries=99`** for timeout,
partial response, oversize response, wrong address, wrong command, malformed
COBS, missing CRC, bad CRC, empty decoded frame, and incomplete writes (0 or 5
bytes reported written). A later same-address call is refused for ambiguous
exchanges; adding a valid late response does not permit another write. Existing
payload-length validation and all motor-owner tests also remain passing.

Valid NACK: one write, an operation error, empty unresolved set, and a subsequent
valid request succeeds. Unrelated addresses remain usable after missing-device
discovery. Response channels 0, 2, and 255 remain non-authoritative. Serialized
concurrent callers cannot consume an unresolved prior reply as a newer operation.

The existing wire tests retain short-move completion, old idle position rejection,
failed read plus late target fencing, malformed stopped value rejection, disabled
owned motor rejection, one MOVE write after lost ACK, and independent completion
for two motor owners on one serialized bus.

An AST comparison verified every non-reader bus/device method unchanged after
resolving the named max-size constant to its existing value 254. This includes
the transaction lock/validation/fence logic, `send_command_no_response`, and
optional `SORTER_PROFILE_BUS` instrumentation. The profiling comment alone was
updated to describe acquisition.

## Validation and isolation

Python **3.12.12**, frozen dependencies, `UV_OFFLINE=1`, Ruff **0.15.21**.
Commands ran from the backend directory, in this order:

```text
uv run --frozen python -m pytest tests/test_mcu_bus_read.py -q -p no:cacheprovider --basetemp <temporary-root>/focused
# 24 passed in 1.14s
uv run --frozen python -m pytest tests/test_mcu_bus_read.py tests/test_hardware_bus.py -q -p no:cacheprovider --basetemp <temporary-root>/affected
# 56 passed in 1.83s
ruff check --no-cache hardware/bus.py tests/test_mcu_bus_read.py tests/test_hardware_bus.py
# All checks passed!
```

The test process used scripted/fake serial only. Bootstrap guards replaced actual
serial constructors and blocked network connect/bind, subprocess launch, and
database access outside a disposable test directory. Local-state/configuration
paths were redirected before imports, bytecode writes disabled, and pytest cache
disabled. No actual serial device, production database, provider, or network was
used by tests. Upstream Git acquisition and review publication are separate
authorized source/publication operations. No full backend suite or other test
module was run.

Independent review inspected the bounded diff, full affected files, source
comparison, reproduction, isolation bootstrap, and passing logs; no findings.
It reused the passing evidence and did not rerun tests. After tests, only original
line endings on unchanged bus.py lines were restored to avoid incidental diff
noise; Python semantics are identical.

## Preservation and patch reconstruction

- Original tracked/untracked inventory: **868 files**.
- **866 unrelated files remain byte-identical**; only the two existing
  allowlisted files changed, and only the new reader test was added.
- Unrelated dirty/untracked status is unchanged. Inventory digest and original
  status entries are recorded in manifest.json; the full hash inventory is
  retained privately. Operational databases were not opened or hashed.
- Implementation branch/HEAD and exact index bytes are unchanged; index is empty.
- The three previously blocked temporary indexes were not accessed.
- No other production path changed, including sorter_interface.py, firmware,
  machine configuration, feeder, classification, transport, distribution,
  Physical C4, Smart Bins, Harvest, local_state, or services.

Patch base: `b604851c01253880d331525884d0dc280a478485`.
Result tree: `615cb66d5d78e4f5737564f3e81b82a1c47ee9cf`.
Patch SHA-256: `8592dec6c2373772fcb30ea0be851a191a4617d8adad470fe275948b542afdfb`.

The patch contains exactly the three paths above. Applying it with `git apply
--cached` to the base tree in a separate fresh index reconstructs the exact result
tree. Source `git diff --check` passes. No implementation commit was made.

## Qualification and succession gate

**No deployment, service restart, firmware operation, serial hardware access,
live-sorter access, physical motion, or physical benchmark occurred.** The live
v0.2.9 / firmware v0.8.1 protected golden and its measurements are supplied task
context; this task did not remeasure or reverify the machine. No physical
performance claim is made. No Harvest runtime integration occurred.

P2C6B remains **BLOCKED**. Stop after C02 publication. The old C03–C13 sequence
is superseded; a new v0.3.0 architecture rebaseline requires a separate task
after C02 review/acceptance.
