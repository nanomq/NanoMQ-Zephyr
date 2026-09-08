#!/usr/bin/env python3
"""Zephyr broker functional test suite (host side).

Runs a functional test matrix against the qemu_x86 NanoMQ broker from
[demo/zephyr_broker](.) — the same client libraries the upstream CI uses
(mosquitto CLI, paho, requests), but pointed at the guest broker and
scoped to what the Zephyr build supports (no TLS, no SQLite, no IPC).

The runner owns the qemu lifecycle: it stops any previous instance,
launches the image built by `west build -b qemu_x86 -d /workdir/build/zephyr_broker
demo/zephyr_broker` inside the `zephyr-tap` container (SLIRP hostfwd binds
in the container's netns), waits for the broker banner, and tears it down
at the end.  Every group runs in its own subprocess so a hang or a crash
cannot poison the next one, and a group that dies with the guest triggers
a broker restart before the suite continues.

Groups (see `--list`):

    mqtt_v311      CI .github/scripts/mqtt_test.py      :1883
    mqtt_v5        CI .github/scripts/mqtt_test_v5.py   :1883
    rest_get       REST GET surface                     :8081
    ws_v311        CI .github/scripts/ws_test.py        :8083
    ws_v5          CI .github/scripts/ws_v5_test.py     :8083
    webhook_smoke  hook_receiver.py + paho              :1883/:18080
    capacity       12 concurrent CONNECTs + QoS1 echo   :1883
    survival       survival_test.py (scaled attack.py)  :1883

Usage (outer host, from anywhere in the repo):

    python3 demo/zephyr_broker/function_test.py              # all groups
    python3 demo/zephyr_broker/function_test.py --list
    python3 demo/zephyr_broker/function_test.py --group ws_v311,ws_v5
    python3 demo/zephyr_broker/function_test.py --no-manage --addr 172.17.0.2
    python3 demo/zephyr_broker/function_test.py --keep-running

Exit status: 0 all groups passed, 1 at least one group failed, 2 the
harness itself could not run (no docker/container, broker won't start).
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

# The CI modules and survival_test.py are imported, not vendored; keep the
# imports from dropping __pycache__ into the tree this runner tests.
sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
CI_SCRIPTS = REPO_ROOT / ".github" / "scripts"

DEFAULT_CONTAINER = "zephyr-tap"
DEFAULT_ADDR = "172.17.0.2"          # zephyr-tap bridge address (dev setup)
DEFAULT_QEMU = (
    "/opt/toolchains/zephyr-sdk-1.0.1/hosttools/sysroots/"
    "x86_64-pokysdk-linux/usr/bin/qemu-system-i386"
)
DEFAULT_KERNEL = "/workdir/build/zephyr_broker/zephyr/zephyr.elf"
DEFAULT_WORKDIR = "/workdir/nanomq"  # repo path inside the container

READY_MARKER = "NanoMQ Broker is started successfully!"

MQTT_PORT = 1883
REST_PORT = 8081
WS_PORT = 8083
WEBHOOK_PORT = 18080

# name, default timeout (s), blurb
GROUPS = [
    ("mqtt_v311", 420,
     "CI mqtt_test.py — sessions, retain, v4/v5 interop"),
    ("mqtt_v5", 600,
     "CI mqtt_test_v5.py — session expiry, user props, $share, topic alias"),
    ("rest_get", 120,
     "REST GET surface on :8081 (routes + /configuration/websocket)"),
    ("ws_v311", 600,
     "CI ws_test.py — MQTT 3.1.1 over nmq-ws :8083"),
    ("ws_v5", 600,
     "CI ws_v5_test.py — MQTT 5 over nmq-ws :8083"),
    ("webhook_smoke", 240,
     "hook_receiver.py receives client_connack + message_publish"),
    ("capacity", 180,
     "12 concurrent CONNECTs + QoS1 echo (connection-pool regression)"),
    ("survival", 300,
     "survival_test.py — scaled-down attack.py load/session churn"),
]
GROUP_NAMES = [g[0] for g in GROUPS]
GROUP_TIMEOUT = {g[0]: g[1] for g in GROUPS}


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ── small helpers ────────────────────────────────────────────────────

def docker(container: str, args, check: bool = False, timeout: float = 120,
           capture: bool = True, detach: bool = False):
    cmd = ["docker", "exec"]
    if detach:
        cmd.append("-d")
    cmd += ["-u", "root", container] + list(args)
    return subprocess.run(cmd, check=check, capture_output=capture,
                          text=True, timeout=timeout)


def broker_alive(addr: str, timeout: float = 5.0) -> bool:
    """Real liveness probe: complete an MQTT CONNECT/CONNACK round trip."""
    try:
        import warnings

        import paho.mqtt.client as mqtt
        from paho.mqtt.client import CallbackAPIVersion

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            c = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION1,
                            client_id="zf-probe")
        c.connect(addr, MQTT_PORT, 10)
        c.loop_start()
        deadline = time.time() + timeout
        while not c.is_connected() and time.time() < deadline:
            time.sleep(0.05)
        ok = c.is_connected()
        c.loop_stop()
        try:
            c.disconnect()
        except Exception:
            pass
        return ok
    except Exception:
        return False


def kill_host_mosquitto_clients() -> int:
    """Drop leftover mosquitto_sub/pub clients.

    They auto-reconnect and can silently steal $share traffic or hold a
    clean-session subscription open, which makes the CI groups flaky.
    The bracket pattern keeps pkill from matching its own command line.
    """
    p = subprocess.run(["pkill", "-f", "mosquitto_[sp]ub"],
                       capture_output=True, text=True)
    return 0 if p.returncode else 1


# ── qemu lifecycle ───────────────────────────────────────────────────

class QemuBroker:
    def __init__(self, args):
        self.container = args.container
        self.addr = args.addr
        self.qemu = args.qemu
        self.kernel = args.kernel
        self.manage = not args.no_manage
        self.keep_running = args.keep_running
        self.serial_log = None
        self.starts = 0

    # -- process control ---------------------------------------------

    def _pids(self) -> int:
        # -f, not -x: pgrep truncates a -x pattern at 15 chars and
        # "qemu-system-i386" is 16, which makes -x match nothing.  The
        # bracket keeps pgrep from matching its own command line.
        p = docker(self.container, ["pgrep", "-c", "-f", "qemu-system-[i]386"])
        if p.returncode != 0 or not p.stdout.strip().isdigit():
            return 0
        return int(p.stdout.strip())

    def kill(self) -> None:
        # Bracket pattern: the pkill command line must not match itself.
        docker(self.container, ["pkill", "-f", "qemu-system-[i]386"])
        deadline = time.time() + 10
        while self._pids() and time.time() < deadline:
            time.sleep(0.3)

    def _new_serial_log(self) -> str:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        n = 1
        while True:
            path = "/tmp/qemu_fz_%s_%d.log" % (stamp, n)
            p = docker(self.container, ["test", "-e", path])
            if p.returncode != 0:          # fresh name: stale logs may be
                return path                # owned by another uid and unwritable
            n += 1

    def start(self) -> str:
        self.kill()
        serial = self._new_serial_log()
        errlog = serial.replace(".log", ".qemu.log")
        forwards = ",".join(
            "hostfwd=tcp:0.0.0.0:%d-:%d" % (p, p)
            for p in (MQTT_PORT, REST_PORT, WS_PORT)
        )
        cmd = (
            "%s -m 32 -cpu qemu32,+nx,+pae,sse,sse2,pni -machine q35 "
            "-device isa-debug-exit,iobase=0xf4,iosize=0x04 -no-reboot "
            "-machine acpi=off -serial file:%s -display none "
            "-netdev user,id=n1,%s -device e1000,netdev=n1 -kernel %s "
            ">%s 2>&1 &"
        ) % (self.qemu, serial, forwards, self.kernel, errlog)
        docker(self.container, ["sh", "-lc", cmd], check=True)
        self.serial_log = serial
        self.starts += 1
        return serial

    def wait_ready(self, serial: str, timeout: float = 90.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if docker(self.container, ["grep", "-q", READY_MARKER,
                                       serial]).returncode == 0:
                return True
            if self._pids() == 0:
                return False           # guest died before the banner
            time.sleep(0.5)
        return False

    # -- high level --------------------------------------------------

    def ensure_up(self) -> bool:
        if not self.manage:
            return broker_alive(self.addr)
        if broker_alive(self.addr):
            return True
        log("    broker not answering — restarting qemu")
        serial = self.start()
        if not self.wait_ready(serial):
            log("    restart failed, serial tail:")
            self.tail_serial(serial, 20)
            return False
        return broker_alive(self.addr)

    def stop(self) -> None:
        if self.manage and not self.keep_running:
            self.kill()

    def tail_serial(self, serial: str, lines: int = 20) -> None:
        if not serial:
            return
        p = docker(self.container, ["tail", "-n", str(lines), serial])
        if p.returncode == 0 and p.stdout.strip():
            log("    --- %s ---" % serial)
            for line in p.stdout.rstrip().splitlines():
                log("    | " + line)


# ── groups (run inside the worker subprocess) ────────────────────────

def _ci_module(name: str):
    if str(CI_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(CI_SCRIPTS))
    __import__(name)
    return sys.modules[name]


def group_mqtt_v311(addr: str, env: dict) -> None:
    """Upstream CI MQTT 3.1.1 suite, retargeted at the guest broker.

    mqtt_test.py builds every mosquitto command from the module-level
    g_url (derived from g_addr/g_port at import time), so all three must
    be overwritten — patching g_addr alone leaves g_url pointing at
    127.0.0.1.
    """
    m = _ci_module("mqtt_test")
    m.g_addr = addr
    m.g_port = MQTT_PORT
    m.g_url = " -h {a} -p {p} ".format(a=addr, p=MQTT_PORT)
    ok = m.mqtt_test()
    assert ok, "mqtt_test() returned %r" % (ok,)


def group_mqtt_v5(addr: str, env: dict) -> None:
    """Upstream CI MQTT 5 suite (session expiry, user properties, $share,
    topic alias, retain-as-published)."""
    m = _ci_module("mqtt_test_v5")
    m.g_addr = addr
    m.g_port = MQTT_PORT
    m.g_url = " -h {a} -p {p} ".format(a=addr, p=MQTT_PORT)
    ok = m.mqtt_v5_test()
    assert ok, "mqtt_v5_test() returned %r" % (ok,)


def group_rest_get(addr: str, env: dict) -> None:
    """REST surface, GET only.

    POST /reload/ (and /write_file) mutate the running broker — the demo
    has no config file to reload, so the suite stays on the read side.
    trust_env=False because the host shell may carry HTTP_PROXY, which
    would send these requests through a proxy that cannot reach the
    container network.
    """
    import requests

    s = requests.Session()
    s.trust_env = False
    base = "http://%s:%d/api/v4" % (addr, REST_PORT)
    paths = [
        "/nodes/",
        "/brokers/",
        "/clients/",
        "/subscriptions/",
        "/reload/",
        "/configuration/",
        "/configuration/websocket",
    ]
    for path in paths:
        r = s.get(base + path, timeout=10)
        assert r.status_code == 200, "%s -> HTTP %d: %s" % (
            path, r.status_code, r.text[:200])
        body = r.json()
        assert isinstance(body, (dict, list)), "%s -> not a JSON object" % path
    ws = s.get(base + "/configuration/websocket", timeout=10).json()
    if isinstance(ws, dict) and "data" in ws:      # {"code":0,"data":{...}}
        ws = ws["data"]
    ws = (ws or {}).get("websocket", {})
    assert ws.get("enable") is True, \
        "websocket.enable is not true in the REST view: %r" % (ws,)
    assert str(WS_PORT) in (ws.get("url") or ""), \
        "websocket.url does not carry port %d: %r" % (WS_PORT, ws)


def group_ws_v311(addr: str, env: dict) -> None:
    """Upstream CI WebSocket 3.1.1 suite.

    ws_test.py's Test.init() defaults host/port to localhost:8083 and
    ws_test() calls it with prot= only, so the defaults are the seam.
    """
    m = _ci_module("ws_test")
    orig_init = m.Test.init

    def init(self, host=addr, port=WS_PORT, tran="websockets", prot=None):
        orig_init(self, host=host, port=port, tran=tran,
                  prot=m.MQTTv5 if prot is None else prot)

    m.Test.init = init
    m.ws_test()


def group_ws_v5(addr: str, env: dict) -> None:
    """Upstream CI WebSocket MQTT 5 suite.

    func() hardcodes connect("localhost", 8083, ...) three times, so the
    seam is paho's Client.connect itself.  The script itself is the
    upstream master version: the copy this tree carried set MaximumPacketSize
    (a CONNECT-only property) on the PUBLISH properties, which paho 2.x
    rejects in the client thread — every v5 publisher died before it
    connected.
    """
    import paho.mqtt.client as pmqtt

    m = _ci_module("ws_v5_test")
    orig_connect = pmqtt.Client.connect

    def connect(self, host, port=1883, keepalive=60, *a, **kw):
        if host == "localhost" and port == WS_PORT:
            host, port = addr, WS_PORT
        return orig_connect(self, host, port, keepalive, *a, **kw)

    pmqtt.Client.connect = connect
    try:
        m.ws_v5_test()
    finally:
        pmqtt.Client.connect = orig_connect


def group_webhook_smoke(addr: str, env: dict) -> None:
    """Webhook forwarder: one CLIENT_CONNACK + one MESSAGE_PUBLISH event.

    The receiver has to run where SLIRP's 10.0.2.2 alias points, i.e. in
    the qemu host's namespace — the container.  The demo's rules are
    CLIENT_CONNACK (all clients) and MESSAGE_PUBLISH on "test/#".
    """
    import paho.mqtt.client as mqtt
    from paho.mqtt.client import CallbackAPIVersion

    container = env["ZF_CONTAINER"]
    workdir = env["ZF_WORKDIR"]
    rec_log = "/tmp/webhook_fz_%d.log" % os.getpid()
    rec_err = rec_log + ".err"
    docker(container, ["pkill", "-f", "hook_receiver[.]py"])
    docker(container, ["rm", "-f", rec_log, rec_err])
    docker(container,
           ["sh", "-lc",
            "exec python3 %s/demo/zephyr_broker/hook_receiver.py --port %d "
            "--out %s >%s 2>&1" % (workdir, WEBHOOK_PORT, rec_log, rec_err)],
           check=True, detach=True)
    try:
        # The forwarder is fire-and-forget: an event POSTed before the
        # receiver binds is lost for good, so wait for the listener.
        deadline = time.time() + 15
        listening = False
        while time.time() < deadline:
            if docker(container, ["sh", "-lc",
                                  "ss -ltn | grep -q :%d" % WEBHOOK_PORT]
                      ).returncode == 0:
                listening = True
                break
            time.sleep(0.3)
        if not listening:
            p = docker(container, ["cat", rec_err])
            raise AssertionError("hook_receiver.py never listened on :%d: %s"
                                 % (WEBHOOK_PORT, (p.stdout or "")[-400:]))

        c = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION1,
                        client_id="zf-webhook-pub")
        c.connect(addr, MQTT_PORT, 30)
        c.loop_start()
        deadline = time.time() + 10
        while not c.is_connected() and time.time() < deadline:
            time.sleep(0.05)
        assert c.is_connected(), "publisher never connected"

        seen = ""
        payload = ""
        for attempt in range(3):
            payload = "zf-webhook-%d-%d" % (int(time.time()), attempt)
            c.publish("test/webhook", payload, 1)
            deadline = time.time() + 10
            while time.time() < deadline:
                p = docker(container, ["cat", rec_log])
                seen = p.stdout if p.returncode == 0 else ""
                if "client_connack" in seen and payload in seen:
                    break
                time.sleep(1)
            if "client_connack" in seen and payload in seen:
                break
        c.loop_stop()
        c.disconnect()
        assert "client_connack" in seen, \
            "no client_connack event in receiver log: %r" % seen[-400:]
        assert "message_publish" in seen and payload in seen, \
            "no message_publish event for %s: %r" % (payload, seen[-400:])
    finally:
        docker(container, ["pkill", "-f", "hook_receiver[.]py"])


def group_capacity(addr: str, env: dict) -> None:
    """Connection-pool regression: N clients connect at the same instant.

    Zephyr hands out one net_context/net_conn per socket; when the pool
    is exhausted its TCP answers with RST and paho reports a dropped
    connection.  The demo's prj.conf sizes both pools to 32 — this group
    is the guard that keeps them sized (see README "Performance notes").
    """
    import paho.mqtt.client as mqtt
    from paho.mqtt.client import CallbackAPIVersion

    n = int(env.get("ZF_CAPACITY_N", "12"))
    barrier = threading.Barrier(n)
    events = [threading.Event() for _ in range(n)]
    rcs = [None] * n
    clients = [None] * n
    errors: list = []

    def worker(i: int) -> None:
        try:
            c = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION1,
                            client_id="zf-cap-%02d" % i,
                            protocol=mqtt.MQTTv311)
            c.on_connect = lambda cl, u, flags, rc, props=None: (
                rcs.__setitem__(i, rc), events[i].set())
            barrier.wait(20)
            c.connect(addr, MQTT_PORT, 60)
            clients[i] = c
            c.loop_start()
            if not events[i].wait(20):
                raise RuntimeError("client %d: no CONNACK within 20 s" % i)
            if rcs[i] != 0:
                raise RuntimeError("client %d: CONNACK rc=%r" % (i, rcs[i]))
        except Exception as e:                      # noqa: BLE001
            errors.append(str(e))

    threads = [threading.Thread(target=worker, args=(i,), name="cap-%d" % i)
               for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    for t in threads:
        assert not t.is_alive(), "capacity worker hung (pool exhaustion?)"
    assert not errors, "concurrent connect failures: %s" % "; ".join(errors)

    # Data plane on top of the freshly established fan-in.
    got = threading.Event()
    topic = "zf/capacity"
    clients[0].on_message = lambda cl, u, msg: got.set()
    clients[0].subscribe(topic, 1)
    time.sleep(0.5)
    clients[0].publish(topic, "pool-ok", 1)
    assert got.wait(15), "QoS1 echo not delivered across %d clients" % n

    for c in clients:
        if c is not None:
            c.loop_stop()
            c.disconnect()


def group_survival(addr: str, env: dict) -> None:
    """Scaled-down upstream attack.py (see survival_test.py)."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import survival_test

    survival_test.survival_test(addr, MQTT_PORT)


GROUP_FUNCS = {
    "mqtt_v311": group_mqtt_v311,
    "mqtt_v5": group_mqtt_v5,
    "rest_get": group_rest_get,
    "ws_v311": group_ws_v311,
    "ws_v5": group_ws_v5,
    "webhook_smoke": group_webhook_smoke,
    "capacity": group_capacity,
    "survival": group_survival,
}


# ── worker entry point ───────────────────────────────────────────────

def worker_main(name: str) -> int:
    env = os.environ
    addr = env["ZF_ADDR"]
    log("[worker %s] broker %s:%d" % (name, addr, MQTT_PORT))
    try:
        GROUP_FUNCS[name](addr, env)
    except BaseException as e:                       # noqa: BLE001
        import traceback
        traceback.print_exc()
        log("[worker %s] FAIL: %s: %s" % (name, type(e).__name__, e))
        # The upstream modules start non-daemon threads that sit in
        # loop_forever(); returning here would leave the interpreter
        # waiting on them until the group timeout.  Exit hard instead.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
    log("[worker %s] PASS" % name)
    # Upstream modules leave non-daemon paho threads behind; do not wait
    # for them to unwind.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


# ── orchestrator ─────────────────────────────────────────────────────

def run_worker(name: str, addr: str, args, timeout: float, attempts: int):
    """Run one group in a subprocess; returns (status, detail)."""
    env = dict(os.environ)
    env.update({
        "ZF_ADDR": addr,
        "ZF_MQTT_PORT": str(MQTT_PORT),
        "ZF_REST_PORT": str(REST_PORT),
        "ZF_WS_PORT": str(WS_PORT),
        "ZF_WEBHOOK_PORT": str(WEBHOOK_PORT),
        "ZF_CONTAINER": args.container,
        "ZF_WORKDIR": args.workdir,
    })
    last = None
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            log("    retry %d/%d" % (attempt - 1, attempts - 1))
        try:
            last = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()),
                 "--worker", name],
                env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            # TimeoutExpired carries raw bytes even in text mode.
            out = ""
            for chunk in (e.stdout, e.stderr):
                if chunk:
                    out += chunk.decode("utf-8", "replace") \
                        if isinstance(chunk, bytes) else chunk
            return ("TIMEOUT", out[-4000:])
        if last.returncode == 0:
            return ("PASS", last.stdout)
    out = (last.stdout or "") + (last.stderr or "")
    return ("FAIL", out[-4000:])


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="function_test.py",
        description="Zephyr broker functional test suite (host side)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("--group", action="append", default=None,
                    metavar="NAME[,NAME...]",
                    help="run only these groups (repeatable)")
    ap.add_argument("--list", action="store_true", help="list groups and exit")
    ap.add_argument("--addr", default=None,
                    help="broker address (default: container IP)")
    ap.add_argument("--container", default=DEFAULT_CONTAINER,
                    help="docker container running qemu (default: %s)"
                         % DEFAULT_CONTAINER)
    ap.add_argument("--qemu", default=DEFAULT_QEMU,
                    help="qemu-system-i386 path inside the container")
    ap.add_argument("--kernel", default=DEFAULT_KERNEL,
                    help="zephyr.elf path inside the container")
    ap.add_argument("--workdir", default=DEFAULT_WORKDIR,
                    help="repo path inside the container")
    ap.add_argument("--no-manage", action="store_true",
                    help="do not start/stop qemu; test the broker already "
                         "running at --addr")
    ap.add_argument("--keep-running", action="store_true",
                    help="leave qemu running after the suite")
    ap.add_argument("--fail-fast", action="store_true",
                    help="stop at the first failing group")
    ap.add_argument("--retry-ws", type=int, default=1,
                    help="extra attempts for the ws groups (default: 1)")
    ap.add_argument("--timeout", type=float, default=None,
                    help="override the per-group timeout (seconds)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="echo each group's output even when it passes")
    ap.add_argument("--worker", default=None, help=argparse.SUPPRESS)
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.worker:
        return worker_main(args.worker)

    if args.list:
        for name, timeout, blurb in GROUPS:
            log("%-14s %4ds  %s" % (name, timeout, blurb))
        return 0

    # ── prerequisites ────────────────────────────────────────────────
    if not CI_SCRIPTS.is_dir():
        log("ERROR: %s not found — run this from a full nanomq checkout"
            % CI_SCRIPTS)
        return 2
    for mod in ("paho.mqtt.client", "requests"):
        try:
            __import__(mod)
        except ImportError:
            log("ERROR: python module %s missing" % mod)
            return 2
    if shutil.which("mosquitto_pub") is None:
        log("ERROR: mosquitto_pub not on PATH (mqtt_v311/mqtt_v5 need it)")
        return 2

    addr = args.addr
    if not args.no_manage:
        p = subprocess.run(
            ["docker", "inspect", "-f",
             "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
             args.container], capture_output=True, text=True)
        if p.returncode != 0:
            log("ERROR: docker container %r not reachable: %s"
                % (args.container, (p.stderr or "").strip()))
            return 2
        if not addr:
            ips = p.stdout.split()
            if not ips:
                log("ERROR: container %r has no IP" % args.container)
                return 2
            addr = ips[0]
    if not addr:
        addr = DEFAULT_ADDR
    args.addr = addr

    selected = []
    if args.group:
        for chunk in args.group:
            for name in chunk.split(","):
                name = name.strip()
                if not name:
                    continue
                if name not in GROUP_FUNCS:
                    log("ERROR: unknown group %r (see --list)" % name)
                    return 2
                if name not in selected:
                    selected.append(name)
        selected = [n for n in GROUP_NAMES if n in selected]
    else:
        selected = list(GROUP_NAMES)

    log("=" * 72)
    log("Zephyr broker functional test suite — broker %s:%d (container %s)"
        % (addr, MQTT_PORT, args.container))
    log("groups: %s" % ", ".join(selected))
    log("=" * 72)

    broker = QemuBroker(args)

    # Leftover clients steal $share traffic and hold clean sessions open.
    if broker.manage:
        killed = kill_host_mosquitto_clients()
        if killed:
            log("killed %d leftover mosquitto_sub/pub client(s)" % killed)
    else:
        log("--no-manage: assuming a broker is already running; leftover "
            "mosquitto clients are NOT cleaned up")

    if broker.manage:
        log("starting qemu ...")
        serial = broker.start()
        if not broker.wait_ready(serial):
            log("ERROR: broker did not come up")
            broker.tail_serial(serial, 25)
            broker.stop()
            return 2
        log("broker ready (%s)" % serial)
    else:
        if not broker_alive(addr):
            log("ERROR: no broker answering at %s:%d" % (addr, MQTT_PORT))
            return 2

    results = []
    failed = 0
    try:
        for idx, name in enumerate(selected, 1):
            if broker.manage and not broker.ensure_up():
                log("ERROR: broker unreachable before group %s — aborting"
                    % name)
                return 2
            timeout = args.timeout or GROUP_TIMEOUT[name]
            attempts = 1 + (args.retry_ws if name.startswith("ws_") else 0)
            log("[%d/%d] %-14s ..." % (idx, len(selected), name))
            t0 = time.time()
            status, output = run_worker(name, addr, args, timeout, attempts)
            dt = time.time() - t0
            results.append((name, status, dt))
            if status == "PASS":
                log("[%d/%d] %-14s PASS  (%.1fs)"
                    % (idx, len(selected), name, dt))
                if args.verbose and (output or "").strip():
                    for line in output.rstrip().splitlines():
                        log("    | " + line)
                continue
            failed += 1
            log("[%d/%d] %-14s %s  (%.1fs)"
                % (idx, len(selected), name, status, dt))
            for line in (output or "").rstrip().splitlines()[-40:]:
                log("    | " + line)
            broker.tail_serial(broker.serial_log, 20)
            if args.fail_fast:
                log("--fail-fast: stopping")
                break
    finally:
        broker.stop()

    log("-" * 72)
    for name, status, dt in results:
        log("%-14s %-8s %6.1fs" % (name, status, dt))
    log("RESULT: pass=%d fail=%d" % (len(results) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(2)
