"""バックエンドのテスト。実際のHevy APIには接続せず、localhostの偽Hevyサーバーを使う。テストデータはすべて架空。

実行: .venv\\Scripts\\python -m unittest discover -s server -p "test_*.py"
"""
import datetime
import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import accounts, manage, security  # noqa: E402
from server.app import create_app  # noqa: E402
from server.config import Config  # noqa: E402
from server.db import connect  # noqa: E402
from server.sync import iso, utcnow  # noqa: E402

KEY_A = "11111111-1111-4111-8111-111111111111"  # 架空のキー
KEY_B = "22222222-2222-4222-8222-222222222222"
KEY_A2 = "33333333-3333-4333-8333-333333333333"  # アカウントAの別キー（変更テスト用）
ACCOUNTS = {KEY_A: ("TEST-ACC-A", "test_a"), KEY_A2: ("TEST-ACC-A", "test_a"), KEY_B: ("TEST-ACC-B", "test_b")}


def workout(wid, days_ago, title="Fictional Workout", updated=None, now=None):
    now = now or utcnow()
    start = now - datetime.timedelta(days=days_ago)
    return {"id": wid, "title": title, "description": None, "start_time": iso(start), "end_time": iso(start + datetime.timedelta(minutes=50)),
            "updated_at": updated or iso(start + datetime.timedelta(minutes=51)), "created_at": iso(start),
            "exercises": [{"index": 0, "title": "Fictional Bench Press (Barbell)", "exercise_template_id": "TEST-0001", "notes": None, "superset_id": None,
                           "sets": [{"index": 0, "type": "warmup", "weight_kg": 40, "reps": 10, "rpe": None},
                                    {"index": 1, "type": "normal", "weight_kg": 60, "reps": 8, "rpe": 8}]}]}


class FakeHevy:
    def __init__(self):
        self.workouts = {a: {} for a, _ in ACCOUNTS.values()}
        self.events = {a: [] for a, _ in ACCOUNTS.values()}  # (ts, event)
        self.templates = [{"id": f"TEST-{i:04d}", "title": f"Fictional Exercise {i}", "type": "weight_reps", "primary_muscle_group": "chest",
                           "secondary_muscle_groups": [], "equipment": "barbell", "is_custom": False, "thumbnail_url": "https://example.invalid/x.png"} for i in range(1, 4)]
        self.fail_plan = []
        self.log = []  # (method, path, api-key)
        self.srv = None

    def start(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                key = self.headers.get("api-key")
                u = urlparse(self.path)
                fake.log.append(("GET", u.path, key))
                if key not in ACCOUNTS:
                    return self._send(401, {"error": "unauthorized"})
                if fake.fail_plan:
                    code = fake.fail_plan.pop(0)
                    if code != 200:
                        return self._send(code, {"error": "x"}, {"Retry-After": "0"} if code == 429 else {})
                acc, uname = ACCOUNTS[key]
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                page, size = int(q.get("page", 1)), int(q.get("pageSize", 10))
                if u.path == "/v1/user/info":
                    return self._send(200, {"data": {"id": acc, "username": uname, "name": "Test " + uname, "is_pro": True}})
                if u.path == "/v1/workouts":
                    items = sorted(fake.workouts[acc].values(), key=lambda w: w["start_time"])  # 古い順（本物の順序は不明という前提で逆に返す必要はない）
                    return self._page(items, page, min(size, 10), "workouts")
                if u.path == "/v1/workouts/events":
                    since = q.get("since", "1970-01-01T00:00:00Z")
                    ev = [e for ts, e in sorted(fake.events[acc], key=lambda x: x[0], reverse=True) if ts >= since]
                    return self._page(ev, page, min(size, 10), "events")
                if u.path == "/v1/exercise_templates":
                    return self._page(fake.templates, page, min(size, 100), "exercise_templates")
                return self._send(404, {})

            def _page(self, items, page, size, key):
                pc = -(-len(items) // size)
                return self._send(200, {"page": page, "page_count": pc, key: items[(page - 1) * size: page * size]})

            def _send(self, code, obj, headers=None):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.srv.server_port}"

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def add(self, acc, w, event=True):
        self.workouts[acc][w["id"]] = w
        if event:
            self.events[acc].append((w["updated_at"], {"type": "updated", "workout": w}))

    def delete(self, acc, wid):
        self.workouts[acc].pop(wid, None)
        self.events[acc].append((iso(utcnow()), {"type": "deleted", "id": wid, "deleted_at": iso(utcnow())}))


class CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class Base(unittest.TestCase):
    min_interval = 0

    def setUp(self):
        self._conns = []
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.fake = FakeHevy()
        base = self.fake.start()
        self.enc = security.generate_key()
        self.cfg = Config(db_path=os.path.join(self.tmp.name, "app.db"), enc_keys=[("k1", _key_for_cfg(self.enc))],
                          public_base_url="", cookie_secure=False, session_ttl_days=1, hevy_base=base, app_env="test", page_delay=0,
                          min_sync_interval=self.min_interval, static_root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.app = create_app(self.cfg, sleep=lambda s: None)
        self.log_handler = CapturingHandler()
        logging.getLogger().addHandler(self.log_handler)
        logging.getLogger().setLevel(logging.DEBUG)

    def tearDown(self):
        logging.getLogger().removeHandler(self.log_handler)
        for c in self._conns:
            c.close()
        self.fake.stop()
        self.tmp.cleanup()

    # --- helpers ---
    def db(self):
        c = connect(self.cfg.db_path)
        self._conns.append(c)
        return c

    def new_client(self):
        return self.app.test_client()

    def make_user(self, username, role="user"):
        """招待→受諾でログイン済みクライアントを作る。(client, csrf, user_id)"""
        conn = self.db()
        token, _ = accounts.create_invite(conn, role, None)
        c = self.new_client()
        r = c.post("/api/invite/accept", json={"token": token, "username": username, "password": "correct horse battery"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        s = c.get("/api/session").get_json()
        return c, s["csrfToken"], s["user"]["id"]

    @staticmethod
    def post(c, csrf, path, data=None, method="post"):
        return getattr(c, method)(path, json=data or {}, headers={"X-CSRF-Token": csrf})

    def connect_hevy(self, c, csrf, key=KEY_A, **extra):
        return self.post(c, csrf, "/api/hevy/connect", {"apiKey": key, **extra})

    def sync(self, c, csrf, **data):
        return self.post(c, csrf, "/api/hevy/sync", data)

    def changes(self, c, after=0, generation=None):
        url = f"/api/hevy/changes?after={after}" + (f"&generation={generation}" if generation is not None else "")
        return c.get(url).get_json()


def _key_for_cfg(text):
    import base64
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class AuthTests(Base):
    def test_bootstrap_admin_only_once_and_no_default_password(self):
        out = __import__("io").StringIO()
        env = {"HEVY_KEY_ENCRYPTION_KEY": self.enc, "DATABASE_PATH": self.cfg.db_path, "APP_ENV": "test"}
        self.assertEqual(manage.main(["bootstrap-admin"], out=out, env=env, home=self.tmp.name), 0)
        self.assertIn("#invite=", out.getvalue())
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)  # 既定のユーザー/パスワードは作らない
        c, csrf, uid = self.make_user("admin_user", role="admin")
        self.assertEqual(manage.main(["bootstrap-admin"], out=__import__("io").StringIO(), err=__import__("io").StringIO(), env=env, home=self.tmp.name), 1)  # 管理者が居れば発行しない

    def test_invite_is_single_use_and_expires(self):
        conn = self.db()
        token, _ = accounts.create_invite(conn, "user", None)
        c = self.new_client()
        self.assertEqual(c.post("/api/invite/accept", json={"token": token, "username": "alice", "password": "correct horse battery"}).status_code, 200)
        self.assertEqual(self.new_client().post("/api/invite/accept", json={"token": token, "username": "bob_x", "password": "correct horse battery"}).status_code, 400)
        t2, _ = accounts.create_invite(conn, "user", None)
        conn.execute("UPDATE invites SET expires_at=?", (iso(utcnow() - datetime.timedelta(hours=1)),))
        self.assertEqual(self.new_client().post("/api/invite/accept", json={"token": t2, "username": "carol", "password": "correct horse battery"}).status_code, 400)
        self.assertEqual(self.new_client().post("/api/invite/accept", json={"token": "bogus", "username": "dave_x", "password": "correct horse battery"}).status_code, 400)

    def test_weak_password_and_bad_username_rejected(self):
        token, _ = accounts.create_invite(self.db(), "user", None)
        c = self.new_client()
        self.assertEqual(c.post("/api/invite/accept", json={"token": token, "username": "alice", "password": "short"}).status_code, 400)
        self.assertEqual(c.post("/api/invite/accept", json={"token": token, "username": "a b!", "password": "correct horse battery"}).status_code, 400)

    def test_unauthenticated_and_csrf_and_origin(self):
        c = self.new_client()
        for path in ("/api/hevy/status", "/api/hevy/changes", "/api/admin/users"):
            self.assertEqual(c.get(path).status_code, 401, path)
        self.assertEqual(c.post("/api/hevy/connect", json={"apiKey": KEY_A}).status_code, 401)
        u, csrf, _ = self.make_user("alice")
        self.assertEqual(u.post("/api/hevy/connect", json={"apiKey": KEY_A}).status_code, 403)  # CSRFトークンなし
        self.assertEqual(u.post("/api/hevy/connect", json={"apiKey": KEY_A}, headers={"X-CSRF-Token": "wrong"}).status_code, 403)
        self.assertEqual(u.post("/api/hevy/connect", json={"apiKey": KEY_A}, headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(u.post("/api/hevy/connect", json={"apiKey": KEY_A}, headers={"X-CSRF-Token": csrf, "Sec-Fetch-Site": "cross-site"}).status_code, 403)
        self.assertEqual(c.post("/api/login", json={"username": "alice", "password": "correct horse battery"}, headers={"Origin": "https://evil.example"}).status_code, 403)

    def test_login_lockout_and_wrong_password(self):
        self.make_user("alice")
        c = self.new_client()
        for _ in range(5):
            self.assertEqual(c.post("/api/login", json={"username": "alice", "password": "wrong password!!"}).status_code, 401)
        self.assertEqual(c.post("/api/login", json={"username": "alice", "password": "correct horse battery"}).status_code, 429)

    def test_admin_only_and_suspend(self):
        admin, acsrf, _ = self.make_user("admin_user", role="admin")
        user, ucsrf, uid = self.make_user("alice")
        self.assertEqual(user.get("/api/admin/users").status_code, 403)
        self.assertEqual(self.post(user, ucsrf, "/api/admin/invites").status_code, 403)
        inv = self.post(admin, acsrf, "/api/admin/invites", {"ttlHours": 1}).get_json()
        self.assertIn("#invite=", inv["inviteUrl"])
        self.assertEqual(self.post(admin, acsrf, f"/api/admin/users/{uid}/suspend").status_code, 200)
        self.assertEqual(user.get("/api/hevy/status").status_code, 401)  # 停止後は即アクセス不可
        self.assertEqual(self.new_client().post("/api/login", json={"username": "alice", "password": "correct horse battery"}).status_code, 401)
        self.assertEqual(self.post(admin, acsrf, f"/api/admin/users/{uid}/activate").status_code, 200)
        self.assertEqual(self.new_client().post("/api/login", json={"username": "alice", "password": "correct horse battery"}).status_code, 200)

    def test_static_allowlist(self):
        c = self.new_client()
        self.assertEqual(c.get("/").status_code, 200)
        self.assertEqual(c.get("/sw.js").status_code, 200)
        for p in ("/server/app.py", "/data/app.db", "/tools/fetch_hevy_exercises.py", "/.git/config", "/requirements.txt"):
            self.assertEqual(c.get(p).status_code, 404, p)
        self.assertIn("no-store", c.get("/api/session").headers["Cache-Control"])


class KeyTests(Base):
    def test_valid_invalid_keys_and_nothing_saved_by_test(self):
        c, csrf, uid = self.make_user("alice")
        self.assertEqual(self.post(c, csrf, "/api/hevy/test", {"apiKey": "not-a-uuid"}).status_code, 400)
        r = self.post(c, csrf, "/api/hevy/test", {"apiKey": "99999999-9999-4999-8999-999999999999"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "hevy_auth")
        ok = self.post(c, csrf, "/api/hevy/test", {"apiKey": KEY_A}).get_json()
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["account"]["username"], "test_a")
        self.assertFalse(c.get("/api/hevy/status").get_json()["connected"])  # 接続テストでは保存されない

    def test_save_change_disconnect_and_key_never_exposed(self):
        c, csrf, uid = self.make_user("alice")
        r = self.connect_hevy(c, csrf)
        self.assertEqual(r.status_code, 200)
        st = r.get_json()
        self.assertEqual(st["keyHint"], KEY_A[-4:])
        everything = r.get_data(as_text=True) + c.get("/api/hevy/status").get_data(as_text=True) + c.get("/api/session").get_data(as_text=True)
        self.assertNotIn(KEY_A, everything)
        # DBには平文で入らず、他利用者IDでは復号できない
        raw = b""
        for f in os.listdir(self.tmp.name):
            with open(os.path.join(self.tmp.name, f), "rb") as fh:
                raw += fh.read()
        self.assertNotIn(KEY_A.encode(), raw)
        row = self.db().execute("SELECT key_enc FROM hevy_connections").fetchone()
        ring = security.KeyRing(self.cfg.enc_keys)
        self.assertEqual(ring.decrypt(row["key_enc"], "hevy-api-key", uid), KEY_A)
        with self.assertRaises(Exception):
            ring.decrypt(row["key_enc"], "hevy-api-key", uid + 1)
        # 同じアカウントの別キーへ変更
        r2 = self.connect_hevy(c, csrf, key=KEY_A2)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.get_json()["keyHint"], KEY_A2[-4:])
        # 解除でキー削除
        d = self.post(c, csrf, "/api/hevy/disconnect", {"deleteHistory": False}).get_json()
        self.assertIn("Hevy側", d["note"])
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM hevy_connections").fetchone()[0], 0)
        self.assertEqual(self.sync(c, csrf).status_code, 409)  # 解除後は同期不可

    def test_key_not_in_logs_or_responses(self):
        c, csrf, uid = self.make_user("alice")
        self.connect_hevy(c, csrf)
        self.fake.add("TEST-ACC-A", workout("W1", 1))
        r = self.sync(c, csrf)
        self.assertNotIn(KEY_A, r.get_data(as_text=True))
        self.fake.fail_plan = [401]
        self.assertNotIn(KEY_A, self.sync(c, csrf).get_data(as_text=True))
        self.assertTrue(all(KEY_A not in line for line in self.log_handler.lines))

    def test_key_only_sent_to_hevy_and_only_get(self):
        c, csrf, uid = self.make_user("alice")
        self.connect_hevy(c, csrf)
        self.fake.add("TEST-ACC-A", workout("W1", 1))
        self.sync(c, csrf)
        self.assertTrue(self.fake.log)
        self.assertTrue(all(m == "GET" for m, _, _ in self.fake.log))
        self.assertTrue(all(p in ("/v1/user/info", "/v1/workouts", "/v1/workouts/events", "/v1/exercise_templates") for _, p, _ in self.fake.log))

    def test_account_change_requires_explicit_choice(self):
        c, csrf, uid = self.make_user("alice")
        self.connect_hevy(c, csrf, key=KEY_A)
        self.fake.add("TEST-ACC-A", workout("WA", 1))
        self.sync(c, csrf)
        r = self.connect_hevy(c, csrf, key=KEY_B)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error"], "account_changed")
        self.assertEqual(c.get("/api/hevy/status").get_json()["account"]["username"], "test_a")  # 変更されていない
        ok = self.connect_hevy(c, csrf, key=KEY_B, accountChange="purge")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.get_json()["account"]["username"], "test_b")
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM hevy_workouts WHERE account_id='TEST-ACC-A'").fetchone()[0], 0)
        self.assertEqual(self.changes(c)["changes"], [])  # 旧アカウントの履歴は混在しない


class SyncTests(Base):
    def setUp(self):
        super().setUp()
        self.c, self.csrf, self.uid = self.make_user("alice")
        self.connect_hevy(self.c, self.csrf)

    def test_initial_sync_pages_range_and_dedupe(self):
        for i in range(23):
            self.fake.add("TEST-ACC-A", workout(f"W{i:02d}", i * 3 + 1))  # 1,4,...,67日前（すべて90日以内）
        self.fake.add("TEST-ACC-A", workout("OLD1", 120))  # 範囲外
        r = self.sync(self.c, self.csrf).get_json()
        self.assertEqual(r["summary"]["added"], 23)
        self.assertEqual(r["summary"]["mode"], "initial")
        listing = [x for x in self.fake.log if x[1] == "/v1/workouts"]
        self.assertEqual(len(listing), 3)  # 10件ずつ3ページ（24件）
        ids = {c["workoutId"] for c in self.changes(self.c)["changes"]}
        self.assertEqual(len(ids), 23)
        self.assertNotIn("OLD1", ids)
        # 再同期しても重複しない
        r2 = self.sync(self.c, self.csrf).get_json()
        self.assertEqual(r2["summary"]["added"], 0)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM hevy_workouts").fetchone()[0], 23)

    def test_incremental_update_delete_and_changes_feed(self):
        self.fake.add("TEST-ACC-A", workout("W1", 2))
        self.fake.add("TEST-ACC-A", workout("W2", 3))
        self.sync(self.c, self.csrf)
        first = self.changes(self.c)
        seq = first["nextSeq"]
        # 更新と削除
        upd = workout("W1", 2, title="Fictional Edited", updated=iso(utcnow() + datetime.timedelta(minutes=5)))
        self.fake.add("TEST-ACC-A", upd)
        self.fake.delete("TEST-ACC-A", "W2")
        r = self.sync(self.c, self.csrf).get_json()
        self.assertEqual((r["summary"]["updated"], r["summary"]["deleted"]), (1, 1))
        feed = self.changes(self.c, after=seq, generation=first["generation"])
        kinds = {c["workoutId"]: c["kind"] for c in feed["changes"]}
        self.assertEqual(kinds, {"W1": "upsert", "W2": "delete"})
        self.assertEqual([c for c in feed["changes"] if c["workoutId"] == "W1"][0]["workout"]["title"], "Fictional Edited")
        # 何も変わらなければ変更なし
        r = self.sync(self.c, self.csrf).get_json()
        self.assertEqual((r["summary"]["added"], r["summary"]["updated"], r["summary"]["deleted"]), (0, 0, 0))

    def test_missing_from_list_is_not_deletion(self):
        self.fake.add("TEST-ACC-A", workout("W1", 2))
        self.sync(self.c, self.csrf)
        del self.fake.workouts["TEST-ACC-A"]["W1"]  # 一覧から消えるが、削除イベントは無い
        self.fake.events["TEST-ACC-A"].clear()
        self.sync(self.c, self.csrf)
        self.assertEqual(self.db().execute("SELECT deleted FROM hevy_workouts WHERE workout_id='W1'").fetchone()[0], 0)

    def test_event_and_older_than_range_is_ignored(self):
        self.sync(self.c, self.csrf)
        self.fake.add("TEST-ACC-A", workout("ANCIENT", 400, updated=iso(utcnow() + datetime.timedelta(minutes=1))))
        r = self.sync(self.c, self.csrf).get_json()
        self.assertEqual(r["summary"]["outOfRange"], 1)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM hevy_workouts").fetchone()[0], 0)

    def test_backfill_adds_older_workouts_once(self):
        self.fake.add("TEST-ACC-A", workout("NEW", 5))
        self.fake.add("TEST-ACC-A", workout("OLD", 200))
        self.sync(self.c, self.csrf)
        r = self.sync(self.c, self.csrf, mode="backfill", days=365).get_json()
        self.assertEqual(r["summary"]["added"], 1)
        r = self.sync(self.c, self.csrf, mode="backfill", days=365).get_json()
        self.assertEqual(r["summary"]["added"] + r["summary"]["updated"], 0)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM hevy_workouts").fetchone()[0], 2)

    def test_rate_limit_retry_and_failure_keeps_cursor(self):
        self.fake.add("TEST-ACC-A", workout("W1", 2))
        self.fake.fail_plan = [429, 500]
        r = self.sync(self.c, self.csrf)
        self.assertEqual(r.status_code, 200)  # 待機して再試行し成功
        before = self.db().execute("SELECT cursor_since, initial_done FROM hevy_state").fetchone()
        self.fake.add("TEST-ACC-A", workout("W2", 1, updated=iso(utcnow() + datetime.timedelta(minutes=1))))
        self.fake.fail_plan = [429] * 20
        r = self.sync(self.c, self.csrf)
        self.assertEqual(r.status_code, 429)
        after = self.db().execute("SELECT cursor_since FROM hevy_state").fetchone()
        self.assertEqual(before["cursor_since"], after["cursor_since"])  # 同期位置は進まない
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM hevy_workouts").fetchone()[0], 1)  # 部分反映もしない
        self.assertTrue(c_status(self.c)["lastError"])
        self.fake.fail_plan = []
        self.assertEqual(self.sync(self.c, self.csrf).get_json()["summary"]["added"], 1)  # 回復後に取りこぼさない
        self.assertIsNone(c_status(self.c)["lastError"])

    def test_auth_and_network_failures_do_not_break_data(self):
        self.fake.add("TEST-ACC-A", workout("W1", 2))
        self.sync(self.c, self.csrf)
        self.fake.fail_plan = [401]
        r = self.sync(self.c, self.csrf)
        self.assertEqual(r.get_json()["error"], "hevy_auth")
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM hevy_workouts WHERE deleted=0").fetchone()[0], 1)

    def test_templates_synced_without_thumbnails(self):
        self.sync(self.c, self.csrf)
        t = self.c.get("/api/hevy/templates").get_json()
        self.assertEqual(len(t["templates"]), 3)
        self.assertTrue(all("thumbnail_url" not in x for x in t["templates"]))

    def test_sync_accepts_empty_events_without_pagination_metadata(self):
        # Hevy 側で空の events 応答から page_count が省略されても、続きは無く
        # 安全に同期完了できる。実データがある場合はページ数を必須のままにする。
        self.sync(self.c, self.csrf)
        original = self.fake.events["TEST-ACC-A"]
        self.fake.events["TEST-ACC-A"] = []
        handler = self.fake.srv.RequestHandlerClass
        original_page = handler._page

        def page_without_metadata(instance, items, page, size, key):
            if key == "events" and not items:
                return instance._send(200, {key: []})
            return original_page(instance, items, page, size, key)

        handler._page = page_without_metadata
        try:
            r = self.sync(self.c, self.csrf)
        finally:
            handler._page = original_page
            self.fake.events["TEST-ACC-A"] = original
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_sync_accepts_camel_case_page_count(self):
        handler = self.fake.srv.RequestHandlerClass
        original_page = handler._page

        def camel_case_page_count(instance, items, page, size, key):
            chunk = items[(page - 1) * size: page * size]
            return instance._send(200, {"page": page, "pageCount": -(-len(items) // size), key: chunk})

        handler._page = camel_case_page_count
        try:
            r = self.sync(self.c, self.csrf)
        finally:
            handler._page = original_page
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_sync_accepts_event_response_aliases(self):
        self.sync(self.c, self.csrf)
        self.fake.add("TEST-ACC-A", workout("W1", 1, updated=iso(utcnow() + datetime.timedelta(minutes=1))))
        handler = self.fake.srv.RequestHandlerClass
        original_page = handler._page

        def event_alias(instance, items, page, size, key):
            if key == "events":
                pc = -(-len(items) // size)
                return instance._send(200, {"page": page, "page_count": pc, "workout_events": items[(page - 1) * size: page * size]})
            return original_page(instance, items, page, size, key)

        handler._page = event_alias
        try:
            r = self.sync(self.c, self.csrf)
        finally:
            handler._page = original_page
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["summary"]["added"], 1)

    def test_sync_treats_workouts_event_response_as_updates(self):
        self.sync(self.c, self.csrf)
        self.fake.add("TEST-ACC-A", workout("W1", 1, updated=iso(utcnow() + datetime.timedelta(minutes=1))))
        handler = self.fake.srv.RequestHandlerClass
        original_page = handler._page

        def event_workouts(instance, items, page, size, key):
            if key == "events":
                pc = -(-len(items) // size)
                workouts = [event["workout"] for event in items if event.get("type") == "updated"]
                return instance._send(200, {"page": page, "page_count": pc, "workouts": workouts})
            return original_page(instance, items, page, size, key)

        handler._page = event_workouts
        try:
            r = self.sync(self.c, self.csrf)
        finally:
            handler._page = original_page
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["summary"]["added"], 1)

    def test_disconnect_keep_or_delete_history_and_generation(self):
        self.fake.add("TEST-ACC-A", workout("W1", 2))
        self.sync(self.c, self.csrf)
        gen = self.changes(self.c)["generation"]
        self.post(self.c, self.csrf, "/api/hevy/disconnect", {"deleteHistory": False})
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM hevy_workouts").fetchone()[0], 1)  # 保持
        self.connect_hevy(self.c, self.csrf)
        self.assertEqual(self.changes(self.c)["generation"], gen)  # 同じアカウントなら継続
        self.post(self.c, self.csrf, "/api/hevy/disconnect", {"deleteHistory": True})
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM hevy_workouts").fetchone()[0], 0)
        self.connect_hevy(self.c, self.csrf)
        self.assertNotEqual(self.changes(self.c, generation=gen)["generation"], gen)
        self.assertTrue(self.changes(self.c, generation=gen)["generationChanged"])


class MinIntervalTests(Base):
    min_interval = 30

    def test_too_soon_is_rejected(self):
        c, csrf, _ = self.make_user("alice")
        self.connect_hevy(c, csrf)
        self.assertEqual(self.sync(c, csrf).status_code, 200)
        r = self.sync(c, csrf)
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.get_json()["error"], "too_soon")

    def test_busy_lock(self):
        c, csrf, uid = self.make_user("alice")
        self.connect_hevy(c, csrf)
        self.db().execute("UPDATE hevy_state SET syncing_since=?", (iso(utcnow()),))
        self.assertEqual(self.sync(c, csrf).status_code, 409)


class IsolationTests(Base):
    def test_users_cannot_see_each_others_data(self):
        a, acsrf, _ = self.make_user("alice")
        b, bcsrf, _ = self.make_user("bobby")
        self.connect_hevy(a, acsrf, key=KEY_A)
        self.connect_hevy(b, bcsrf, key=KEY_B)
        self.fake.add("TEST-ACC-A", workout("A1", 2))
        self.fake.add("TEST-ACC-B", workout("B1", 2))
        self.sync(a, acsrf)
        self.sync(b, bcsrf)
        self.assertEqual([c["workoutId"] for c in self.changes(a)["changes"]], ["A1"])
        self.assertEqual([c["workoutId"] for c in self.changes(b)["changes"]], ["B1"])
        self.assertEqual(a.get("/api/hevy/status").get_json()["account"]["username"], "test_a")
        self.assertEqual(b.get("/api/hevy/status").get_json()["account"]["username"], "test_b")
        # 同じHevyアカウントを2人が登録しても、同期位置と履歴は別々
        c2, c2csrf, _ = self.make_user("carol")
        self.connect_hevy(c2, c2csrf, key=KEY_A2)
        self.assertEqual(self.changes(c2)["changes"], [])
        self.sync(c2, c2csrf)
        self.assertEqual(len(self.changes(c2)["changes"]), 1)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM hevy_workouts").fetchone()[0], 3)
        # Bが暗号化済みキーをAのIDで復号できない
        rows = self.db().execute("SELECT user_id, key_enc FROM hevy_connections ORDER BY user_id").fetchall()
        ring = security.KeyRing(self.cfg.enc_keys)
        with self.assertRaises(Exception):
            ring.decrypt(rows[0]["key_enc"], "hevy-api-key", rows[1]["user_id"])


def c_status(c):
    return c.get("/api/hevy/status").get_json()


if __name__ == "__main__":
    unittest.main()
