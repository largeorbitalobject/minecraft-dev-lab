import base64
import json
import os
import re
import tempfile
import time
import unittest

import helpers  # noqa: F401
from mcdevlab_viewer import remote, remote_agent as RA


class ReadOnly(unittest.TestCase):
    def test_agent_source_cannot_write_or_run_anything(self):
        with open(RA.__file__) as fh:
            src = fh.read()
        code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
        for bad in (r"\bos\.(remove|unlink|rmdir|rename|replace|makedirs|mkdir|system|popen|chmod|chown|symlink|link|truncate|utime)\b",
                    r"\bshutil\b", r"\bsubprocess\b", r"\bsocket\b", r"\burllib\b", r"\bhttp\b", r"\bos\.exec", r"\beval\(", r"\bexec\(",
                    r"open\([^)]*['\"][wax+]"):
            self.assertIsNone(re.search(bad, code), bad)
        # every open() is binary read
        for ln in code.splitlines():
            if re.search(r"\bopen\(", ln):
                self.assertIn('"rb"', ln)


class Ops(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        def put(path, data, mode="wb"):
            with open(path, mode) as fh:
                fh.write(data)
        self.put = put
        for p, data in (("run1/input.json", b'{"a":1}'), ("run1/probe/result.txt", b"PASS x"), ("run1/probe/a.png", b"\x89PNG" + bytes(range(256)) * 4),
                        ("run1/game/huge.dat", b"x" * 100), ("run2/probe/result.txt", b"FAIL y"), ("top.txt", b"t")):
            os.makedirs(os.path.dirname(os.path.join(self.d, p)), exist_ok=True)
            put(os.path.join(self.d, p), data)

    def test_tree_prunes_and_inlines_and_skips_known(self):
        r = RA.op_tree({"root": self.d, "maxdepth": 3, "include": ["*/input.json", "*/probe/*"], "exclude_dirs": ["game"], "inline": ["*/probe/*.txt", "*/input.json"]})
        files = {f["p"] for f in r["files"]}
        self.assertEqual(files, {"run1/input.json", "run1/probe/result.txt", "run1/probe/a.png", "run2/probe/result.txt"})
        self.assertEqual(r["inline"]["run1/probe/result.txt"], "PASS x")
        self.assertNotIn("run1/probe/a.png", r["inline"])
        known = {f["p"]: [f["s"], f["m"]] for f in r["files"]}
        r2 = RA.op_tree({"root": self.d, "maxdepth": 3, "include": ["*/probe/*"], "inline": ["*/probe/*.txt"], "known": known})
        self.assertEqual(r2["inline"], {})                              # unchanged files are not re-sent
        self.assertEqual(RA.op_tree({"root": self.d + "/nope"})["exists"], False)

    def test_glob_semantics(self):
        self.assertTrue(RA._match("a/b/progress.jsonl", "progress.jsonl"))       # no slash: basename at any depth
        self.assertTrue(RA._match("run/probe/x.png", "*/probe/*.png"))
        self.assertFalse(RA._match("remote-slots/run/exit.json", "*/exit.json"))  # '*' does not cross '/'

    def test_since_filter(self):
        old = os.path.join(self.d, "top.txt")
        os.utime(old, (time.time() - 5000, time.time() - 5000))
        r = RA.op_tree({"root": self.d, "maxdepth": 1, "include": ["*.txt"], "since_s": 60})
        self.assertEqual(r["files"], [])

    def test_read_and_tail_incremental(self):
        p = os.path.join(self.d, "grow.jsonl")
        self.put(p, b"l1\nl2\n")
        t1 = RA.op_tail({"path": p, "max_bytes": 100, "offset": 0})
        self.assertEqual(t1["text"], "l1\nl2\n")
        self.put(p, b"l3\n", "ab")
        t2 = RA.op_tail({"path": p, "max_bytes": 100, "offset": t1["end"]})
        self.assertEqual(t2["text"], "l3\n")
        self.put(p, b"x\n")                                # truncated/rotated
        t3 = RA.op_tail({"path": p, "max_bytes": 100, "offset": 50})
        self.assertTrue(t3["reset"])
        self.assertFalse(RA.op_read({"paths": [self.d + "/missing"]})["files"][self.d + "/missing"]["exists"])
        self.assertEqual(RA.op_tail({"path": self.d + "/missing"}), {"exists": False})

    def test_live_picks_newest_group_and_tails(self):
        os.utime(os.path.join(self.d, "run1/probe/result.txt"), (time.time() - 1000, time.time() - 1000))
        r = RA.op_live({"root": self.d, "maxdepth": 3, "group": "first", "exclude_dirs": ["game"],
                        "roles": [{"role": "result", "include": ["*/probe/*.txt"], "tail_bytes": 50}, {"role": "shot", "include": ["*/probe/*.png"], "tail_bytes": 0}]})
        self.assertEqual(r["group"], "run2")
        self.assertEqual(r["tails"]["run2/probe/result.txt"]["text"], "FAIL y")

    def test_frames_roundtrip_with_binary_and_errors(self):
        req = {"ops": [{"id": "f", "op": "fetch", "files": [{"root": self.d + "/run1/probe", "rel": "a.png"}, {"root": self.d, "rel": "nope.bin"}]},
                       {"id": "bad", "op": "zzz"}]}
        results, blobs, errs = remote.LocalTransport().call(req, 5)
        self.assertEqual(blobs[0], b"\x89PNG" + bytes(range(256)) * 4)
        self.assertIn(1, errs)
        self.assertIn("unknown op", results["bad"]["error"])
        payload = remote.build_payload(req)
        ns = {}
        head, _src = payload.split(b"\n", 1)
        self.assertEqual(json.loads(base64.b64decode(re.search(rb'"(.*)"', head).group(1))), req)

    def test_noise_before_marker_is_ignored(self):
        raw = b"Welcome to Windows banner\r\n" + b"MCDV\n" + RA._frame_json({"ok": True, "results": {}}) + b"F0 3\nabc"
        reply, blobs, _ = remote.parse_frames(raw)
        self.assertEqual(blobs[0], b"abc")
        with self.assertRaises(remote.TransportError):
            remote.parse_frames(b"nothing here")


if __name__ == "__main__":
    unittest.main()
