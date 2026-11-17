#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi
"""A stand-in for the Argus core's /api/enroll, for test/probe_live_test.sh.

It does what the core does for a probe that enrolls: a token is good once, the probe's CSR is
signed by the CA, and the answer carries the certificate, the CA and the proxy's name. A token it
doesn't know, or has seen before, gets the core's own refusal.

Usage: fake_core.py PORT CA_CRT CA_KEY PROXY_NAME TOKEN...
"""
import json
import os
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT, CA_CRT, CA_KEY, NAME = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
TOKENS = set(sys.argv[5:])


class Core(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def answer(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            req = {}
        if self.path != "/api/enroll":
            return self.answer(404, {"error": "not found"})
        token = req.get("token")
        if token not in TOKENS:
            return self.answer(401, {"error": "invalid, used, or expired enrollment token"})
        TOKENS.discard(token)
        with tempfile.TemporaryDirectory() as d:
            csr, crt = os.path.join(d, "probe.csr"), os.path.join(d, "probe.crt")
            with open(csr, "w") as f:
                f.write(req.get("csr") or "")
            r = subprocess.run(["openssl", "x509", "-req", "-in", csr, "-CA", CA_CRT, "-CAkey", CA_KEY,
                                "-CAcreateserial", "-CAserial", os.path.join(d, "ca.srl"), "-out", crt, "-days", "2"],
                               capture_output=True)
            if r.returncode != 0:
                return self.answer(400, {"error": "the CSR could not be signed"})
            with open(crt) as f:
                cert = f.read()
        with open(CA_CRT) as f:
            ca = f.read()
        self.answer(200, {"certificate": cert, "ca": ca, "proxy_name": NAME, "core_host": "127.0.0.1",
                          "probe_token": "", "checkin_url": ""})


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", PORT), Core).serve_forever()
