# NanoMQ broker on Zephyr (qemu_x86)

Runs the full NanoMQ broker core (`nanomq/nanomq/` application sources +
the NanoNNG submodule `nng/`, the nng fork with the nanolib/MQTT stack) on
Zephyr, demonstrated on `qemu_x86` with a SLIRP user-net bridge so real
host MQTT clients (mosquitto) can reach the broker.

Protocol support is the standard NanoMQ TCP/WebSocket surface: MQTT
v3.1.1 and v5, QoS 0/1/2, retained messages, will messages, $SYS, and the
`nmq-tcp://`/`nmq-ws://` transports — no TLS, no QUIC, no SQLite (built
out of the NanoNNG library, same as the NanoNNG Zephyr port itself).

## What was needed to get here

Three layers of changes, kept apart deliberately:

| Layer | Change | Where |
|---|---|---|
| NanoNNG port | already Zephyr-ready (branch `develop`, no TLS/Parquet) | `nng/` submodule |
| NanoMQ core | POSIX-only bits gated behind `__ZEPHYR__`: signal handlers in `apps/broker.c`, `ptrace.h` in `nanomq.c`, `W_OK` file-log check in `mqtt_api.c` | this repo |
| broker bug fix | `nano_nni_lmq_fini`/`nano_nni_lmq_resize` freed the in-struct `lmq_buf` when the rlmq never grew (guard `lmq_alloc > 0`, mirroring `core/lmq.c`) — heap corruption on client disconnect on Zephyr | `nng/` submodule (commit in this branch) |
| Demo | this directory | `demo/zephyr_broker` |

Two NanoNNG no-FS platform gaps are filled by small stubs compiled into
the app ([src/process_stub.c](src/process_stub.c),
[src/nng_plat_stub.c](src/nng_plat_stub.c)) instead of patching the
submodule.

## Environment prerequisite — Zephyr e1000 driver patch

**Required.** The qemu_x86 emulated NIC is an Intel e1000.  QEMU's e1000
device model clears RCTL after reset — including **RCTL_BAM** (Broadcast
Accept Mode, bit 15), which real hardware defaults to set.  Zephyr's
`eth_e1000` driver programs `RCTL_EN | RCTL_MPE` but never sets BAM, so
**all broadcast frames (ARP!) are silently dropped by the device model**
and SLIRP cannot deliver the first TCP connection (no ARP resolution).
Upstream Zephyr has the same bug (checked on main, 2026-06).

Patch (two lines on a Zephyr ≥ 4.x tree, e.g. 4.4 @ 11a87708d41):

```c
// drivers/ethernet/eth_e1000_priv.h, next to RCTL_MPE
#define RCTL_BAM    (1 << 15) /* Broadcast Accept Mode */

// drivers/ethernet/eth_e1000.c, e1000_eth_init() RCTL write
iow32(dev, RCTL, RCTL_EN | RCTL_MPE | RCTL_BAM | DT_INST_PROP(inst, rdmts) << RDMTS_OFFSET);
```

## Build

Needs a Zephyr 4.x SDK workspace with the NanoNNG submodule checked out:

```sh
cd <nanomq-repo>
git submodule update --init nng        # NanoNNG fork, branch develop
west build -b qemu_x86 demo/zephyr_broker
```

The NanoNNG library is built by an ExternalProject
([demo/cmake/nanonng_external.cmake](../cmake/nanonng_external.cmake))
into `build/zephyr_broker/nanonng_build/`, mirroring the NanoNNG
`zephyr_mqtt` demo's build.  RAM footprint of the linked image: ~1.35 MB
(1 MB of it the libc malloc arena — see below).

### Why the big malloc arena

qemu_x86 enables the MMU, so Zephyr's libc uses a **16 KB malloc arena**
by default (`CONFIG_COMMON_LIBC_MALLOC_ARENA_SIZE`); nng's Zephyr
platform allocator is plain `malloc()` and per-pipe queue growth alone
(`rlmq`, `msq_len * 8` bytes) exceeds 16 KB, making every allocation
fail.  [prj.conf](prj.conf) raises the arena to 1 MB.  `CONFIG_HEAP_MEM_POOL_SIZE`
only serves `k_malloc()` and is irrelevant to nng.

## Run

```sh
west build -t run        # or launch qemu by hand, e.g.:
qemu-system-i386 -m 32 -machine q35 -cpu qemu32,+nx,+pae,sse,sse2,pni \
  -no-reboot -machine acpi=off \
  -netdev user,id=n1,hostfwd=tcp:0.0.0.0:1883-:1883 \
  -device e1000,netdev=n1 \
  -kernel build/zephyr_broker/zephyr/zephyr.elf
```

The guest broker listens on `10.0.2.15:1883` (static IP set in
[prj.conf](prj.conf)); SLIRP forwards host `tcp:1883` to it.  Use a
different `hostfwd` port if 1883 is taken.

## Acceptance

Host-side mosquitto clients against the forwarded port:

```sh
./accept.sh [host] [port]      # default 127.0.0.1 1883
```

Covers QoS0/1/2 pub/sub, retained messages, will messages (client
SIGKILLed — note mosquitto rejects `-k < 5`), MQTT v5 pub/sub with
user-properties and the response-topic/correlation-data roundtrip.
Expect `RESULT: pass=7 fail=0`.

Notes from bring-up that are easy to trip on again:

* mosquitto `-k` (keepalive) minimum is **5** — `-k 2` makes the client
  exit immediately without connecting.
* A SIGKILLed client's will is only published when the broker sees the
  socket close; `kill -9` the client itself, not a `timeout` wrapper
  around it.
