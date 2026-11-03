#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi

# Tests for argus_corepost.py, how the probe's jobs post results: python3 deploy/probe-image/tests/test_corepost.py
import http.server
import io
import json
import os
import socket
import sys
import threading
import unittest
from contextlib import redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "externalscripts"))
import argus_corepost  # noqa: E402


class Core(http.server.BaseHTTPRequestHandler):
    posts = []
    status = 204

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        Core.posts.append((self.path, self.headers.get("Authorization"), self.headers.get("User-Agent"), json.loads(body)))
        self.send_response(Core.status)
        self.end_headers()
        if Core.status >= 300:
            self.wfile.write(b'{"error":"unknown diagnostic"}')

    def log_message(self, *a):
        pass


class Post(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), Core)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = "http://core.example.lan:%d/api/probes/diag-results" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        Core.posts, Core.status = [], 204
        self.real = socket.getaddrinfo

        def musl(host, port, family=0, *a, **kw):
            # What an Alpine probe sees when the AAAA half of the name is NXDOMAIN: a lookup for both
            # families fails as a whole, the IPv4-only one answers.
            if host == "core.example.lan":
                if family == socket.AF_INET:
                    return self.real("127.0.0.1", port, family, *a, **kw)
                raise socket.gaierror(-2, "Name does not resolve")
            return self.real(host, port, family, *a, **kw)
        socket.getaddrinfo = musl

    def tearDown(self):
        socket.getaddrinfo = self.real

    def test_reaches_the_core_when_the_aaaa_half_is_nxdomain(self):
        self.assertTrue(argus_corepost.post(self.url, "tok", {"id": 7, "result": {"done": True}}))
        self.assertEqual(Core.posts, [("/api/probes/diag-results", "Bearer tok", "argus-probe", {"id": 7, "result": {"done": True}})])

    def test_a_refused_post_says_why(self):
        Core.status = 404
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertFalse(argus_corepost.post(self.url, "tok", {"id": 7}, tries=2, wait=0, who="argus_diag"))
        self.assertEqual(len(Core.posts), 2)
        self.assertIn("argus_diag: posting to %s failed (try 2 of 2): HTTP 404" % self.url, err.getvalue())
        self.assertIn("unknown diagnostic", err.getvalue())
        self.assertNotIn("tok", err.getvalue())

    def test_quiet_when_asked(self):
        Core.status = 404
        err = io.StringIO()
        with redirect_stderr(err):
            argus_corepost.post(self.url, "tok", {"id": 7}, log=False)
        self.assertEqual(err.getvalue(), "")

    def test_a_name_with_no_address_says_so(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertFalse(argus_corepost.post("http://nowhere.invalid:9/x", "tok", {}))
        self.assertIn("gaierror", err.getvalue())


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
