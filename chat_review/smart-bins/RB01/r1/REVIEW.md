# RB01 r1 — v0.3.0 MCU transaction safety

## Result and scope

Created `C:\Users\abria\Downloads\sorter-v2-88-update-pcb\smart-bins-v030-integration-20260929` on local branch `smart-bins-v030-integration`
from exact upstream `sorter/stable/v0.3.0` commit `8951f914eb34778139b0df42f59f3957abbc2f8a`,
tree `21e805b60da575ff7468fcc3570a517d80f01cd0`. Initial status and index were empty; all
1569 tracked/nonignored files were hashed before edits
(inventory digest `c64ac11890567a14f94c43e3183b79a28115eaf75ac5f9e455f4b886f28c727f`).

The implementation changes only `hardware/bus.py`; the two changed or new tests
are under `software/sorter/backend/tests/`. The resulting source tree is
`ab7e7461a69efc1db74faac2c8e8f3318ca10341`. Implementation HEAD remains the v0.3.0 commit,
with no implementation commit or push and an empty index. This is source and
fake-serial test qualification only.

## Pre-fix reproduction and correction

Against pristine v0.3.0 bus source, a deterministic fake serial port delivered
only the first six bytes of a valid response. One `send_command` with
`retries=2` wrote the same command **3 times** and ended with
`Partial response (missing terminator), got 6 bytes`. The unmodified bus had no unresolved-address fence.

With RB01, the identical probe wrote **1 time** and left address
0 unresolved. A later valid-looking reply to that address cannot authorize
another write. The implementation holds one bus lock through the complete
send, read, validation and ambiguity bookkeeping. `retries` remains a public
compatibility argument but never triggers another command write. Timeout,
partial or oversized response, COBS/length/CRC/payload mismatch, wrong
address or command, and incomplete `serial.write` all fence the attempted
address. An unrelated address remains usable.

A fully framed valid-address NACK for the requested command with bit 7 set
raises `MCUBusError` without a resend or unresolved fence; the next valid
same-address request succeeds. Reply channel is preserved in `Message` and is
not used as a motor identity check; observed values 0, 2 and 255 pass when the
rest of the frame is valid. The declared payload length must match the entire
received payload exactly.

The v0.3.0 chunk reader, including `in_waiting`, bounded `read()`, first-zero
termination, split-transfer reassembly, `MAX_FRAME_SIZE = 254`, and
`MAX_FRAME_READ_S = 1.0`, is retained. Its executable AST is unchanged;
only its historical docstring was clarified. `close`,
`send_command_no_response`, and `MCUDevice.send_command` executable ASTs are
also unchanged. No old runtime, feeder, Physical C4, Smart Bin, or Harvest
hooks were restored. No C01 files were changed.

## Exact changed paths

| Path | Before SHA-256 | After SHA-256 |
| --- | --- | --- |
| `software/sorter/backend/hardware/bus.py` | `cb85690ca36035f42d0d0812ad76ab997025064d30174dbf935b46b0792b3466` | `8e572f8d7dad63a5b0e92a01f96408af87099f2a8ff4f82133697bc784f69615` |
| `software/sorter/backend/tests/test_hardware_bus.py` | `absent` | `dc7337b30729f3d9d298992c23e78d58149869e3045d82ffcb2e90604d69f671` |
| `software/sorter/backend/tests/test_mcu_bus_read.py` | `b2e8725f87c49032881e353752be3abfca50fab068cbfc3ada24644fb58fbc6a` | `737211075faf67a8bf983b98f2f89462d3a57a613d660e16ffac7529e80efd95` |

`RB01.patch` SHA-256 is `2446b1f100b777f98cded5e41d935c583258c68b3ec0ec7ae77425b46fd4bfc3`. It contains exactly the
three paths above. An isolated Git index read exact base tree
`21e805b60da575ff7468fcc3570a517d80f01cd0`, applied the patch and wrote exact result tree
`ab7e7461a69efc1db74faac2c8e8f3318ca10341`; the implementation index was not used.

## Focused validation

Run from `software/sorter/backend` with v0.3.0's frozen environment:

- Pre-edit `uv run --frozen python -m pytest tests/test_mcu_bus_read.py -q`:
  **5 passed**.
- Post-edit same reader command: **6 passed**.
- `uv run --frozen python -m pytest tests/test_mcu_bus_read.py tests/test_hardware_bus.py -q`:
  **24 passed**.
- `uv run --frozen ruff check hardware/bus.py tests/test_mcu_bus_read.py tests/test_hardware_bus.py`:
  **all checks passed**.
- `git diff --check`: passed.

The bounded tests cover complete and split replies, slow per-read scheduling,
trailing terminators, silence, partial and maximum-size frames, every listed
invalid exchange, no resend with `retries=99`, late-reply fencing, address
isolation, NACK, channel values and a waiting concurrent caller. No full
backend suite was run.

## Preservation and boundary

The old candidate remains on `sorting-flow-candidate` at
`c4e298ebd1a153001355df3d3b0c41cc5446ac8f`. Its 869 tracked/nonignored
files are byte identical before and after, inventory digest
`d4ae072299bc0590115451435325d135ae22dc0b375d459b14827c45733af03e`; its index SHA-256 remains
`f9b14e028a87a6722fb563af357225d867cb8b5cf701d77603f113e9d94dc7d6`, staged paths remain empty and status is
unchanged. The three previously blocked temporary Git indexes were not
accessed.

The new integration branch remains at `8951f914eb34778139b0df42f59f3957abbc2f8a` with an empty,
byte-identical index (`18c7474d1a3a7fc2b9b8b894f6340ca5e8e607d78b0de3a154156c3c08b1027f`). Its only modifications
are the three listed paths, uncommitted. No live sorter, real serial port,
firmware, hardware motion, production database, deployment or implementation
push was involved. P2C6B remains blocked; RB02 was not started.

Semantic references: P2C6P-B r1 `d451afdb5a93d3e3b7bc8d84c57a15f005b23f7b`,
C02 review `6c723deba5f4fa20a23a6a6706089a51356034ef`, accepted old
result tree `615cb66d5d78e4f5737564f3e81b82a1c47ee9cf`.
