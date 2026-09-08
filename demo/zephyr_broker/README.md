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

The one POSIX-only piece nng itself cannot build on Zephyr is the
broker's `process.c` (fork/kill/chdir) — a small app-side stand-in
([src/process_stub.c](src/process_stub.c)) provides its symbols.  The
NanoNNG no-FS file gap this demo originally papered over
(`nni_plat_file_exists/size` missing from `zephyr_file.c`, which broke
linking `nanolib`'s file.c/log.c) is fixed in the submodule instead:
both `zephyr_file.c` branches now implement the probes (commit
`21daab5`), exposed as a public API — `nng_file_exists` /
`nng_file_size` in `nng.h` (commit `c66e0cb`).

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

Needs a Zephyr 4.x SDK workspace with the NanoNNG submodule checked out.
In this repo's dev setup everything runs inside the `zephyr-tap` docker
container (image `ghcr.io/zephyrproject-rtos/zephyr-build:main`): the
repo is bind-mounted at `/workdir/nanomq` inside a west workspace rooted
at `/workdir` (Zephyr 4.4 @ 11a87708d41 in `/workdir/zephyr`, SDK at
`/opt/toolchains/zephyr-sdk-1.0.1`).  Use `-u root` — the bind-mounted
files are owned by the host uid (1001), while the container's default
user is uid 1000 and cannot write them.

```sh
# host → container
docker exec -u root zephyr-tap sh -lc '
  cd /workdir/nanomq &&
  git submodule update --init nng &&            # NanoNNG fork, branch develop
  ZEPHYR_TOOLCHAIN_VARIANT=zephyr \
  ZEPHYR_SDK_INSTALL_DIR=/opt/toolchains/zephyr-sdk-1.0.1 \
    west build -b qemu_x86 -d /workdir/build/zephyr_broker demo/zephyr_broker'
```

(In a plain west workspace the two env vars are unnecessary and the build
dir defaults to the repo's `build/zephyr_broker/`.)  The NanoNNG library
is built by an ExternalProject
([demo/cmake/nanonng_external.cmake](../cmake/nanonng_external.cmake))
into `<build-dir>/nanonng_build/` (here
`/workdir/build/zephyr_broker/nanonng_build/`), mirroring the NanoNNG
`zephyr_mqtt` demo's build.  RAM footprint of the linked image: ~2.4 MB
of the qemu_x86 31 MB RAM (≈1 MB of it the libc malloc arena — see
below).

### Why the big malloc arena

qemu_x86 enables the MMU, so Zephyr's libc uses a **16 KB malloc arena**
by default (`CONFIG_COMMON_LIBC_MALLOC_ARENA_SIZE`); nng's Zephyr
platform allocator is plain `malloc()` and per-pipe queue growth alone
(`rlmq`, `msq_len * 8` bytes) exceeds 16 KB, making every allocation
fail.  [prj.conf](prj.conf) raises the arena to 1 MB.  `CONFIG_HEAP_MEM_POOL_SIZE`
only serves `k_malloc()` and is irrelevant to nng.

## Run

The broker boots inside the `zephyr-tap` container — SLIRP `hostfwd` ports
live in the container's network namespace, not on the outer host (client
addressing table below).  Launch qemu headless, serial console to a file:

```sh
# host → container.  Stop any previous instance first; the bracket pattern
# keeps pkill from matching its own command line (PORTING_ZEPHYR.md §7-5).
docker exec -u root zephyr-tap pkill -f "qemu-system-[i]386" || true

# launch detached; serial console → /tmp/qemu3.log in the container
docker exec -u root zephyr-tap sh -lc '
  /opt/toolchains/zephyr-sdk-1.0.1/hosttools/sysroots/x86_64-pokysdk-linux/usr/bin/qemu-system-i386 \
    -m 32 -cpu qemu32,+nx,+pae,sse,sse2,pni -machine q35 \
    -device isa-debug-exit,iobase=0xf4,iosize=0x04 -no-reboot -machine acpi=off \
    -serial file:/tmp/qemu3.log -display none \
    -netdev user,id=n1,hostfwd=tcp:0.0.0.0:1883-:1883,hostfwd=tcp:0.0.0.0:8081-:8081 \
    -device e1000,netdev=n1 \
    -kernel /workdir/build/zephyr_broker/zephyr/zephyr.elf &'
```

(qemu is the SDK's hosttools build.  `west build -t run` is equivalent —
the runner applies the `hostfwd` pair from [prj.conf](prj.conf)'s
`CONFIG_NET_QEMU_USER_EXTRA_ARGS` automatically — but keeps the serial
console on stdio and occupies the terminal.  In the hand-launched form
above both port forwards must be listed explicitly, exactly as here; the
kernel path matches the `-d /workdir/build/zephyr_broker` of the Build
step.)

**Confirm the broker came up** — within a second or two the log shows the
interface address, the HTTP/REST listener and the broker banner.  Log
timestamps are real wall-clock (UTC): at boot, main.c seeds Zephyr's
CLOCK_REALTIME from the QEMU-emulated CMOS RTC (QEMU initialises it from
the host clock; without this the nanolib log module — log.c formats
`time(NULL)` — would print the 1970 epoch):

```
$ docker exec zephyr-tap tail -f /tmp/qemu3.log
rtc: CMOS clock 2026-09-08 06:49:01 UTC, realtime seeded
net: iface 0x1991b4 dev=eth0 up=1
net: ipv4 10.0.2.15
2026-09-08 06:49:01 [0] WARN  ... broker: NanoMQ (ver 0.25.1) Serving HTTP Server on http://(null):8081
NanoMQ Broker is started successfully!
```

The guest broker listens on `10.0.2.15:1883` (static IP set in
[prj.conf](prj.conf)); SLIRP forwards container `tcp:1883` to it.  Pick a
different `hostfwd` port (e.g. `11883`) if 1883 is taken on the host.
Stop the broker by running the first (pkill) command again; a rebuild
must be followed by a relaunch.

### Where to run the clients (docker dev setup)

SLIRP forwards bind inside the container, so the address a client must
use depends on where it runs — mosquitto and curl exist only on the
outer host, python3 only inside the container:

| Client | Run on | Address |
|---|---|---|
| `mosquitto_sub` / `mosquitto_pub`, `accept.sh` | outer host | `127.0.0.1:1883` via the socat forward below, or `<container-ip>:1883` |
| `mqtt_accept.py`, `hook_receiver.py` | inside container | `127.0.0.1:1883`, `127.0.0.1:18080` |
| REST `curl` | outer host | `http://127.0.0.1:8081` via the socat forward, or `http://<container-ip>:8081` |

```sh
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' zephyr-tap   # → e.g. 172.17.0.2
```

To use plain `localhost` from the outer host instead of the container IP,
forward the two ports on the host (the container itself has no published
ports).  Needs socat; the forwards die with the host (or when the
container IP changes) and are restarted the same way:

```sh
socat TCP-LISTEN:1883,reuseaddr,fork,bind=127.0.0.1 TCP:172.17.0.2:1883 &
socat TCP-LISTEN:8081,reuseaddr,fork,bind=127.0.0.1 TCP:172.17.0.2:8081 &
```

## Acceptance

Host-side mosquitto clients against the forwarded port — run on the outer
host, targeting the container IP (see the table in "Run"; the container
image has no mosquitto):

```sh
./accept.sh <container-ip> 1883      # e.g. ./accept.sh 172.17.0.2 1883
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

## Extended scenarios (session / keepalive / $SYS / REST / webhook)

The acceptance script above covers the protocol surface; the scenarios
below (verification record: `PORTING_ZEPHYR.md` §9-4/6/7/10) need packet
control the mosquitto CLI does not give you (clean=0 without auto-reconnect,
MQTT 5 session-expiry, arbitrary keepalive), so they are driven by
[`mqtt_accept.py`](mqtt_accept.py) — a stdlib-only raw-socket MQTT 3.1.1/5
client with machine-friendly `CONNACK/SUBACK/MSG/MATCH/EXIT` output.  Run
these inside the container (it has python3 but no mosquitto), e.g. from
the host:

```sh
docker exec zephyr-tap python3 /workdir/nanomq/demo/zephyr_broker/mqtt_accept.py 127.0.0.1 1883 ...
```

or `cd demo/zephyr_broker` inside the container and use the shorter
`python3 mqtt_accept.py ...` forms below:

```sh
# persistent session, MQTT 5, 30 s session-expiry: reconnect within the
# window and the offline QoS1 message is delivered (session_present=1)
python3 mqtt_accept.py 127.0.0.1 1883 sub --proto 5 --clean 0 --expiry 30 \
    --topic v5/offline --qos 1 --expect v5-offline --hold 25
# keepalive timeout: a silent client with --keepalive 2 is kicked after
# ~5.6 s (1.5 x keepalive + the 1 s qos_duration tick); watch the broker
# log or REST for the disconnect
python3 mqtt_accept.py 127.0.0.1 1883 connect --keepalive 2 --hold 30
# $SYS: subscribe from a second client to see the online/offline pair
python3 mqtt_accept.py 127.0.0.1 1883 expect --topic '$SYS/brokers/client_status/#' \
    --expect online --expect offline --hold 20
```

These need the demo's default build flags (`CONFIG_BROKER_REST_API`,
`CONFIG_BROKER_WEBHOOK`, `CONFIG_BROKER_LOG_DEBUG`); the REST and webhook
switches were added on top of the original demo to make the §9-4 path
exercisable (Kconfig options wired to main.c overrides, see
[Kconfig](Kconfig)).  The broker also overrides `qos_duration` to 1 s
(conf default 10 s) so keepalive/session-expiry checks tick promptly.

REST is served on `tcp:8081` (second SLIRP hostfwd in [prj.conf](prj.conf))
with auth off.  curl exists on the outer host only — use the container IP
(see the table in "Run"):

```sh
curl -s http://<container-ip>:8081/api/v4/clients   # top-level key is "data"
```

Webhook events are POSTed to the QEMU-host alias `10.0.2.2`.  Run
[`hook_receiver.py`](hook_receiver.py) there before publishing to
`test/#` to see them:

```sh
docker exec -d zephyr-tap python3 /workdir/nanomq/demo/zephyr_broker/hook_receiver.py \
    --port 18080 --out /tmp/webhook.log
```

One event per connect (`client_connack` — clientid, proto_ver, keepalive)
and one per `test/#` publish (`message_publish` — ts, topic, qos, payload).

## Performance notes (qemu/SLIRP)

Best-effort numbers (see PORTING_ZEPHYR.md §9-10 for the record):

* QoS0 one-way forwarding reaches ~1.1-1.4 k msg/s with zero loss.
* Request/response exchanges (QoS1 PUBACK, PINGREQ, SUBACK) cost a flat
  ~110 ms each — Zephyr's TCP delayed-ACK (`ACK_DELAY = K_MSEC(100)`)
  holds the ACK for ~100 ms on small segments.  This is a stack
  characteristic, not a broker defect; QoS0 one-way throughput is
  unaffected.
* With `CONFIG_BROKER_LOG_DEBUG=y` the serial console becomes the
  bottleneck (~380 log lines/s) and throughput drops to ~150-250 msg/s;
  measure on a non-DEBUG build.

SLIRP is a proxy network: absolute numbers need re-measuring on real
hardware/network.  For the docker dev setup, MQTT/REST are reachable
inside the `zephyr-tap` container (hostfwd binds in its namespace), not
on the outer host.
