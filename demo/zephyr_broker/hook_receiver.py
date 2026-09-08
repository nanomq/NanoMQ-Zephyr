#!/usr/bin/env python3
"""Tiny HTTP receiver for NanoMQ webhook acceptance.

NanoMQ's webhook forwarder is fire-and-forget: it POSTs the event JSON to
conf->web_hook.url and never reads the reply.  This receiver just logs one
line per POST body ("<epoch> <path> <body>") to stdout and optionally to a
file, then answers 200.

Run it wherever QEMU's SLIRP host alias 10.0.2.2 points — i.e. on the host
running qemu.  In the docker dev setup that host is the zephyr-tap
container itself:

    docker exec -d zephyr-tap python3 /workdir/nanomq/demo/zephyr_broker/hook_receiver.py --port 18080 --out /tmp/webhook.log

Usage: hook_receiver.py [--port 18080] [--out FILE]
"""

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--out", default=None,
                    help="append each POST body as a line to this file")
    args = ap.parse_args()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n).decode("utf-8", "replace")
            line = "%d %s %s" % (time.time(), self.path, body)
            print(line, flush=True)
            if args.out:
                with open(args.out, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            payload = json.dumps({"code": 0}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):  # keep the console quiet
            pass

    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    srv.serve_forever()


if __name__ == "__main__":
    main()
