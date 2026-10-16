"""fetch_hevy_exercises.py のテスト。実際のHevy APIには接続せず、localhostの偽サーバーを使う。

テストデータはすべて架空（id は TEST- で始まる）。実際のHevy種目IDではない。
実行: py -m unittest discover -s tools -p "test_*.py"
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(__file__))
import fetch_hevy_exercises as F  # noqa: E402

FAKE_KEY = "TEST-KEY-00000000-0000-0000-0000-000000000000"


def fake_templates(n, start=1):
    return [{"id": f"TEST-{i:04d}", "title": f"Test Exercise {i} (Fictional)", "type": "weight_reps",
             "primary_muscle_group": "chest", "secondary_muscle_groups": ["triceps"],
             "equipment": "barbell", "is_custom": i % 5 == 0, "thumbnail_url": None, "future_field": i}
            for i in range(start, start + n)]


class Scenario:
    def __init__(self, items, status_plan=None):
        self.items = items
        self.status_plan = list(status_plan or [])  # 先頭から順に消費するHTTPステータス
        self.requests = []
        self.methods = set()


def make_server(scn):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            scn.methods.add("GET")
            scn.requests.append(self.path)
            if self.headers.get("api-key") != FAKE_KEY:
                return self._send(401, {"error": "unauthorized"})
            if scn.status_plan:
                code = scn.status_plan.pop(0)
                if code != 200:
                    return self._send(code, {"error": "x"}, {"Retry-After": "0"} if code == 429 else {})
            u = urlparse(self.path)
            if u.path == "/v1/user/info":
                return self._send(200, {"data": {"id": "TEST-USER-1"}})
            q = parse_qs(u.query)
            page, size = int(q["page"][0]), int(q["pageSize"][0])
            chunk = scn.items[(page - 1) * size: page * size]
            pc = max(1, -(-len(scn.items) // size))
            return self._send(200, {"page": page, "page_count": pc, "exercise_templates": chunk})

        def _send(self, code, obj, headers=None):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def run_main(base, out_path, key=FAKE_KEY, extra=()):
    out, err = io.StringIO(), io.StringIO()
    env = {"HEVY_API_KEY": key} if key is not None else {}
    code = F.main(["--out", out_path, "--base-url", base, "--no-jitter", *extra], env=env,
                  sleep=lambda s: None, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "hevy-exercises.json")

    def tearDown(self):
        self.tmp.cleanup()

    def serve(self, scn):
        srv, base = make_server(scn)
        self.addCleanup(srv.shutdown)
        return base

    def test_multi_page_and_dedupe(self):
        items = fake_templates(5) + [fake_templates(1, start=3)[0]] + fake_templates(2, start=6)  # TEST-0003 が重複
        scn = Scenario(items)
        base = self.serve(scn)
        code, out, err = run_main(base, self.out, extra=["--page-size", "3"])
        self.assertEqual(code, 0, err)
        data = json.load(open(self.out, encoding="utf-8"))
        ids = [e["hevyId"] for e in data["exercises"]]
        self.assertEqual(ids, [f"TEST-{i:04d}" for i in range(1, 8)])
        self.assertEqual(data["count"], 7)
        self.assertEqual(data["duplicatesSkipped"], 1)
        self.assertEqual(data["pagesFetched"], 3)
        self.assertEqual(data["format"], F.FORMAT)
        self.assertEqual(data["schemaVersion"], 1)
        self.assertTrue(data["fetchedAt"].endswith("Z"))
        self.assertEqual(data["exercises"][0]["extra"], {"future_field": 1})  # 未知のフィールドも保持
        self.assertEqual(data["exercises"][0]["primaryMuscleGroup"], "chest")
        self.assertEqual(scn.methods, {"GET"})
        self.assertTrue(data["accountKey"] and "TEST-USER-1" not in data["accountKey"])

    def test_auth_failure_keeps_existing_file(self):
        open(self.out, "w", encoding="utf-8").write('{"keep":"me"}')
        base = self.serve(Scenario(fake_templates(2)))
        code, out, err = run_main(base, self.out, key="WRONG-KEY-123")
        self.assertEqual(code, 1)
        self.assertEqual(open(self.out, encoding="utf-8").read(), '{"keep":"me"}')
        self.assertNotIn("WRONG-KEY-123", out + err)
        self.assertEqual([f for f in os.listdir(self.tmp.name) if f.endswith(".tmp")], [])

    def test_failure_on_second_page_keeps_existing_file(self):
        open(self.out, "w", encoding="utf-8").write('{"keep":"me"}')
        scn = Scenario(fake_templates(6), status_plan=[200, 403])
        base = self.serve(scn)
        code, _, err = run_main(base, self.out, extra=["--page-size", "3"])
        self.assertEqual(code, 1)
        self.assertEqual(open(self.out, encoding="utf-8").read(), '{"keep":"me"}')

    def test_rate_limit_is_retried(self):
        scn = Scenario(fake_templates(2), status_plan=[429, 500, 200])
        base = self.serve(scn)
        code, out, err = run_main(base, self.out)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.load(open(self.out, encoding="utf-8"))["count"], 2)
        self.assertIn("再試行", out)

    def test_persistent_rate_limit_fails(self):
        scn = Scenario(fake_templates(2), status_plan=[429] * 10)
        base = self.serve(scn)
        code, _, err = run_main(base, self.out)
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.out))

    def test_malformed_item_is_rejected(self):
        items = fake_templates(2) + [{"title": "no id"}]
        base = self.serve(Scenario(items))
        open(self.out, "w", encoding="utf-8").write("OLD")
        code, _, err = run_main(base, self.out)
        self.assertEqual(code, 1)
        self.assertEqual(open(self.out, encoding="utf-8").read(), "OLD")

    def test_empty_result_is_rejected(self):
        base = self.serve(Scenario([]))
        code, _, err = run_main(base, self.out)
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.out))

    def test_api_key_never_in_output_or_logs(self):
        base = self.serve(Scenario(fake_templates(4)))
        code, out, err = run_main(base, self.out, extra=["--page-size", "2"])
        self.assertEqual(code, 0)
        self.assertNotIn(FAKE_KEY, out + err)
        self.assertNotIn(FAKE_KEY, open(self.out, encoding="utf-8").read())

    def test_missing_key_is_config_error(self):
        code, _, err = run_main("http://127.0.0.1:1", self.out, key=None)
        self.assertEqual(code, 2)
        self.assertIn("HEVY_API_KEY", err)

    def test_remote_plain_http_is_refused(self):
        code, _, err = run_main("http://example.com", self.out)
        self.assertEqual(code, 1)
        self.assertNotIn(FAKE_KEY, err)

    def test_only_get_is_used(self):
        scn = Scenario(fake_templates(2))
        base = self.serve(scn)
        run_main(base, self.out)
        self.assertEqual(scn.methods, {"GET"})


if __name__ == "__main__":
    unittest.main()
