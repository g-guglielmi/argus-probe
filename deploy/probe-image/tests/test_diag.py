#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi

# Tests for argus_diag.py, the probe's diagnostics: python3 deploy/probe-image/tests/test_diag.py
import importlib.util
import os
import socket
import struct
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "externalscripts"))  # argus_diag imports argus_corepost
spec =importlib.util.spec_from_file_location("diag", os.path.join(HERE, "..", "externalscripts", "argus_diag.py"))
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


class Clean(unittest.TestCase):
    def test_refuses_what_could_read_as_an_option(self):
        for req in ({"tool": "ping", "target": "-f"}, {"tool": "ping", "target": "a;reboot"}, {"tool": "ping", "target": "$(id)"},
                    {"tool": "ping", "target": ""}, {"tool": "telnet", "target": "web1"}, {"tool": "dns", "target": "web1", "type": "ANY"},
                    {"tool": "dns", "target": "web1", "type": "PTR"}, {"tool": "dns", "target": "web1", "server": "dns.example.lan"}):
            with self.assertRaises(ValueError, msg=req):
                diag.clean(req)

    def test_fills_in_defaults(self):
        self.assertEqual(diag.clean({"tool": "Ping", "target": "nas1.example.lan."}), {"tool": "ping", "target": "nas1.example.lan", "count": 5})
        self.assertEqual(diag.clean({"tool": "ping", "target": "10.0.0.20", "count": 99})["count"], 20)
        self.assertEqual(diag.clean({"tool": "dns", "target": "10.0.0.20", "type": "ptr"})["type"], "PTR")
        self.assertEqual(diag.clean({"tool": "dns", "target": "example.lan"})["type"], "both")

    def test_a_bad_request_still_ends_with_why(self):
        res = diag.run({"tool": "ping", "target": "-f"})
        self.assertTrue(res["done"])
        self.assertEqual(res["error"], "the target must be a host name or an address")


class Traceroute(unittest.TestCase):
    def test_parse_hop(self):
        self.assertIsNone(diag.parse_hop("traceroute to 10.0.0.20 (10.0.0.20), 30 hops max, 46 byte packets"))
        self.assertEqual(diag.parse_hop(" 1  10.0.0.1  0.412 ms"), {"n": 1, "addr": "10.0.0.1", "ms": 0.412})
        self.assertEqual(diag.parse_hop(" 4  *"), {"n": 4})
        self.assertEqual(diag.parse_hop("12  192.0.2.1  4.100 ms !H"), {"n": 12, "addr": "192.0.2.1", "ms": 4.1, "note": "host unreachable"})

    def test_raw_marks_silent_hops(self):
        raw = diag.trace_raw("nas1.example.lan", "10.0.0.20", [{"n": 1, "addr": "10.0.0.1", "name": "gateway.example.lan", "ms": 4.0}, {"n": 2}])
        self.assertIn(" 1  gateway.example.lan (10.0.0.1)  4.000 ms", raw)
        self.assertIn(" 2  *", raw)


class Ping(unittest.TestCase):
    def test_raw_reads_like_busybox(self):
        raw = diag.ping_raw("nas1.example.lan", "10.0.0.20", [{"seq": 0, "from": "10.0.0.20", "ms": 0.412}, {"seq": 1}, {"seq": 2, "from": "10.0.0.20", "ms": 0.9}], True)
        for want in ("64 bytes from 10.0.0.20: seq=0 time=0.412 ms", "no reply for seq=1",
                     "3 packets transmitted, 2 packets received, 33% packet loss", "round-trip min/avg/max = 0.412/0.656/0.900 ms"):
            self.assertIn(want, raw)

    def test_reads_busybox_reply_line(self):
        m = diag.PING_TIME.search("PING 10.0.0.20 (10.0.0.20): 56 data bytes\n64 bytes from 10.0.0.20: seq=0 ttl=64 time=0.412 ms\n")
        self.assertEqual((m.group(1), m.group(2)), ("10.0.0.20", "0.412"))


def name_bytes(n):
    return b"".join(bytes([len(p)]) + p.encode() for p in n.split(".")) + b"\x00"


class FakeDNS:
    """Answers A with a CNAME then 10.0.0.20 (their owner names point back at the question),
    and everything else with NXDOMAIN."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                q, frm = self.sock.recvfrom(1500)
            except OSError:
                return
            qid = struct.unpack(">H", q[:2])[0]
            qtype = struct.unpack(">H", q[-4:-2])[0]
            question = q[12:]
            if qtype == 1:
                target = name_bytes("nas1.example.lan")
                ans = (b"\xc0\x0c" + struct.pack(">HHIH", 5, 1, 60, len(target)) + target
                       + b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 300, 4) + bytes([10, 0, 0, 20]))
                msg = struct.pack(">HHHHHH", qid, 0x8180, 1, 2, 0, 0) + question + ans
            else:
                msg = struct.pack(">HHHHHH", qid, 0x8183, 1, 0, 0, 0) + question
            self.sock.sendto(msg, frm)


class DNS(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDNS()
        self.old = diag.DNS_PORT
        diag.DNS_PORT = self.fake.port

    def tearDown(self):
        diag.DNS_PORT = self.old
        self.fake.sock.close()

    def test_answers_and_rcodes(self):
        rcode, ans = diag.query_server("127.0.0.1", "nas.example.lan", "A")
        self.assertEqual(rcode, 0)
        self.assertEqual([(a["type"], a["value"], a["ttl"]) for a in ans], [("CNAME", "nas1.example.lan", 60), ("A", "10.0.0.20", 300)])
        rcode, ans = diag.query_server("127.0.0.1", "nas.example.lan", "AAAA")
        self.assertEqual((rcode, ans), (3, []))

    def test_differ_sees_an_answer_the_checks_miss(self):
        server = [{"type": "A", "value": "10.0.0.20"}, {"type": "AAAA", "error": "NXDOMAIN: the name does not exist"}]
        self.assertTrue(diag.differ(server, [{"type": "A", "error": "no such name"}]))
        self.assertFalse(diag.differ(server, [{"type": "A", "value": "10.0.0.20"}, {"type": "AAAA", "error": "no such name"}]))

    def test_reverse_zone(self):
        self.assertEqual(diag.reverse_zone("10.0.0.20"), "20.0.0.10.in-addr.arpa")
        self.assertTrue(diag.reverse_zone("2001:db8::1").endswith(".8.b.d.0.1.0.0.2.ip6.arpa"))


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
