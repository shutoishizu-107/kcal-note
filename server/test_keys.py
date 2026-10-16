"""暗号化鍵の保護・初回設定/通常起動の分離のテスト。実際のHevy APIには接続しない。テストデータは架空。

実行: .venv\\Scripts\\python -W ignore -m unittest server.test_keys
"""
import hashlib
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import accounts, manage, run, security  # noqa: E402
from server.app import create_app  # noqa: E402
from server.config import ConfigError, load_config, parse_env_file  # noqa: E402
from server.db import connect, init_db  # noqa: E402
from server.test_server import KEY_A, FakeHevy, workout  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class KeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = self.tmp.name
        self.envfile = os.path.join(self.home, ".env.local")
        self.db = os.path.join(self.home, "data", "app.db")

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        code = manage.main(list(argv), out=out, err=err, env={} if env is None else env, home=self.home)
        return code, out.getvalue(), err.getvalue()

    def key_in_file(self):
        return parse_env_file(self.envfile).get("HEVY_KEY_ENCRYPTION_KEY")

    def make_db_with_encrypted_key(self, key_b64):
        """別の鍵で暗号化済みAPIキーを持つ既存DBを用意する。"""
        init_db(self.db)
        conn = connect(self.db)
        token, _ = accounts.create_invite(conn, "user", None)
        uid = accounts.accept_invite(conn, token, "alice", "correct horse battery")
        import base64
        ring = security.KeyRing([("k1", base64.urlsafe_b64decode(key_b64 + "=" * (-len(key_b64) % 4)))])
        conn.execute("INSERT INTO hevy_connections(user_id, key_enc, key_hint, hevy_account_id, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                     (uid, ring.encrypt(KEY_A, "hevy-api-key", uid), KEY_A[-4:], "TEST-ACC-A", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
        conn.close()

    # --- 初回設定 ---
    def test_first_init_creates_key_and_admin_without_printing_key(self):
        code, out, err = self.cli("init")
        self.assertEqual(code, 0, err)
        key = self.key_in_file()
        self.assertTrue(key and len(key) >= 40)
        self.assertNotIn(key, out + err)
        self.assertIn("#invite=", out)
        self.assertTrue(os.path.exists(self.db))
        self.assertEqual(parse_env_file(self.envfile).get("APP_ENV"), "development")

    def test_rerun_keeps_key_and_does_not_recreate_admin(self):
        self.cli("init")
        key, before = self.key_in_file(), open(self.envfile, "rb").read()
        conn = connect(self.db)
        accounts.accept_invite(conn, self._latest_invite(conn), "admin_user", "correct horse battery")
        invites_before = conn.execute("SELECT COUNT(*) FROM invites").fetchone()[0]
        conn.close()
        code, out, err = self.cli("init")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.key_in_file(), key)
        self.assertEqual(open(self.envfile, "rb").read(), before)
        self.assertIn("保持", out)
        self.assertIn("作成済み", out)
        conn = connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM invites").fetchone()[0], invites_before)
        conn.close()

    def _latest_invite(self, conn):
        # テスト用: 発行直後のトークンはハッシュのみ保存のため、同じ内容の新しい招待を作って使う
        token, _ = accounts.create_invite(conn, "admin", None)
        return token

    def test_existing_env_entries_are_kept_and_file_is_backed_up(self):
        original = "APP_ENV=development\nCUSTOM_SETTING=keep-me"
        with open(self.envfile, "w", encoding="utf-8", newline="\n") as f:
            f.write(original)
        code, out, err = self.cli("init")
        self.assertEqual(code, 0, err)
        vals = parse_env_file(self.envfile)
        self.assertEqual(vals["CUSTOM_SETTING"], "keep-me")
        self.assertEqual(vals["APP_ENV"], "development")
        self.assertTrue(vals["HEVY_KEY_ENCRYPTION_KEY"])
        baks = [n for n in os.listdir(self.home) if n.startswith(".env.local.bak-")]
        self.assertEqual(len(baks), 1)
        self.assertEqual(open(os.path.join(self.home, baks[0]), encoding="utf-8").read(), original)

    def test_empty_key_entry_is_not_overwritten(self):
        with open(self.envfile, "w", encoding="utf-8") as f:
            f.write("HEVY_KEY_ENCRYPTION_KEY=\n")
        before = sha(self.envfile)
        code, out, err = self.cli("init")
        self.assertEqual(code, 1)
        self.assertEqual(sha(self.envfile), before)

    # --- 鍵の欠落・不一致 ---
    def test_missing_key_with_existing_encrypted_data_refuses_and_changes_nothing(self):
        self.make_db_with_encrypted_key(security.generate_key())
        before = sha(self.db)
        code, out, err = self.cli("init")
        self.assertEqual(code, 1)
        self.assertIn("復旧", err)
        self.assertIn("reset-hevy-keys", err)
        self.assertFalse(os.path.exists(self.envfile))  # 鍵を勝手に生成していない
        with self.assertRaises(ConfigError) as cm:
            load_config({}, self.home)
        self.assertIn("復旧", str(cm.exception))
        self.assertEqual(self.cli("check")[0], 2)
        self.assertEqual(sha(self.db), before)

    def test_wrong_key_refuses_to_start_and_leaves_db_untouched(self):
        self.make_db_with_encrypted_key(security.generate_key())
        wrong = security.generate_key()
        before = sha(self.db)
        env = {"HEVY_KEY_ENCRYPTION_KEY": wrong}
        code, out, err = self.cli("check", env=env)
        self.assertEqual(code, 2)
        self.assertIn("復号できません", err)
        self.assertNotIn(wrong, out + err)
        with self.assertRaises(ConfigError) as cm:
            create_app(load_config(env, self.home))
        self.assertNotIn(wrong, str(cm.exception))
        self.assertEqual(sha(self.db), before)
        self.assertEqual(self.cli("init", env=env)[0], 1)
        self.assertEqual(sha(self.db), before)

    def test_invalid_key_value_gives_secret_free_error(self):
        bad = "not-a-valid-key-value-123"
        with self.assertRaises(ConfigError) as cm:
            load_config({"HEVY_KEY_ENCRYPTION_KEY": bad}, self.home)
        self.assertNotIn(bad, str(cm.exception))

    # --- 再起動後も復号できる ---
    def test_key_saved_survives_restart_and_sync_still_decrypts(self):
        fake = FakeHevy()
        base = fake.start()
        self.addCleanup(fake.stop)
        fake.add("TEST-ACC-A", workout("W1", 1))
        env = {"HEVY_API_BASE": base, "MIN_SYNC_INTERVAL_SECONDS": "0", "HEVY_PAGE_DELAY": "0"}
        self.assertEqual(self.cli("init", env=env)[0], 0)
        key_before = self.key_in_file()

        app1 = create_app(load_config(env, self.home), sleep=lambda s: None)
        conn = connect(self.db)
        token, _ = accounts.create_invite(conn, "user", None)
        conn.close()
        c1 = app1.test_client()
        self.assertEqual(c1.post("/api/invite/accept", json={"token": token, "username": "alice", "password": "correct horse battery"}).status_code, 200)
        csrf = c1.get("/api/session").get_json()["csrfToken"]
        self.assertEqual(c1.post("/api/hevy/connect", json={"apiKey": KEY_A}, headers={"X-CSRF-Token": csrf}).status_code, 200)

        # 再起動（同じ .env.local を読み直す。鍵は変わらない）
        self.assertEqual(self.cli("init", env=env)[0], 0)
        self.assertEqual(self.key_in_file(), key_before)
        app2 = create_app(load_config(env, self.home), sleep=lambda s: None)
        c2 = app2.test_client()
        self.assertEqual(c2.post("/api/login", json={"username": "alice", "password": "correct horse battery"}).status_code, 200)
        csrf2 = c2.get("/api/session").get_json()["csrfToken"]
        r = c2.post("/api/hevy/sync", json={}, headers={"X-CSRF-Token": csrf2})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["summary"]["added"], 1)
        self.assertNotIn(key_before, r.get_data(as_text=True))

    # --- 設定の共有・優先順位・パス ---
    def test_server_and_cli_share_config_and_paths_regardless_of_cwd(self):
        self.cli("init")
        a = load_config({}, self.home)
        old = os.getcwd()
        other = tempfile.mkdtemp()
        try:
            os.chdir(other)
            b = load_config({}, self.home)
            with mock.patch.dict(os.environ, {"KCAL_HOME": self.home}):
                c = load_config()
        finally:
            os.chdir(old)
        self.assertEqual(a.db_path, b.db_path)
        self.assertEqual(a.db_path, c.db_path)
        self.assertEqual(a.db_path, os.path.abspath(self.db))
        self.assertEqual(a.enc_keys, c.enc_keys)
        rel = load_config({"HEVY_KEY_ENCRYPTION_KEY": security.generate_key(), "DATABASE_PATH": "custom/x.db"}, self.home)
        self.assertEqual(rel.db_path, os.path.abspath(os.path.join(self.home, "custom", "x.db")))

    def test_process_env_overrides_env_local(self):
        k1, k2 = security.generate_key(), security.generate_key()
        with open(self.envfile, "w", encoding="utf-8") as f:
            f.write(f"HEVY_KEY_ENCRYPTION_KEY={k1}\nAPP_ENV=development\n")
        from server.config import parse_enc_keys, effective_env
        self.assertEqual(parse_enc_keys(effective_env({"HEVY_KEY_ENCRYPTION_KEY": k2}, self.home))[0][1], load_config({"HEVY_KEY_ENCRYPTION_KEY": k2}, self.home).enc_keys[0][1])
        self.assertNotEqual(load_config({}, self.home).enc_keys[0][1], load_config({"HEVY_KEY_ENCRYPTION_KEY": k2}, self.home).enc_keys[0][1])
        self.assertEqual(load_config({"HEVY_KEY_ENCRYPTION_KEY": ""}, self.home).enc_keys[0][0], "k1")  # 空のプロセス変数は無視して .env.local を使う

    def test_normal_start_never_generates_key_or_admin(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"KCAL_HOME": self.home}), mock.patch("sys.stderr", err):
            for k in ("HEVY_KEY_ENCRYPTION_KEY", "HEVY_KEY_ENCRYPTION_KEYS"):
                os.environ.pop(k, None)
            code = run.main()
        self.assertEqual(code, 2)
        self.assertIn("setup.ps1", err.getvalue())
        self.assertFalse(os.path.exists(self.envfile))
        self.assertFalse(os.path.exists(self.db))

    # --- 運用コマンド ---
    def test_rotate_and_reset_make_backups_and_keep_users(self):
        k_old = security.generate_key()
        self.make_db_with_encrypted_key(k_old)
        k_new = security.generate_key()
        env = {"HEVY_KEY_ENCRYPTION_KEYS": f"k2:{k_new},k1:{k_old}"}
        code, out, err = self.cli("rotate-keys", env=env)
        self.assertEqual(code, 0, err)
        self.assertIn("1件", out)
        self.assertTrue(any(n.endswith(".bak") for n in os.listdir(os.path.join(self.home, "data", "backups"))))
        self.assertEqual(self.cli("check", env={"HEVY_KEY_ENCRYPTION_KEYS": f"k2:{k_new}"})[0], 0)  # 旧鍵なしでも復号できる（鍵ID k2 を保ったまま）
        # 紛失時の最終手段: フラグなしでは何もしない
        self.assertEqual(self.cli("reset-hevy-keys", env={})[0], 2)
        self.assertEqual(self.cli("reset-hevy-keys", "--confirm-key-lost", env={})[0], 0)
        conn = connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM hevy_connections").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
        conn.close()

    # --- Git ---
    def test_env_local_is_git_ignored_and_no_secrets_tracked(self):
        for name in (".env.local", ".env.local.bak-20260101-000000"):
            r = subprocess.run(["git", "check-ignore", "-q", name], cwd=REPO)
            self.assertEqual(r.returncode, 0, name)
        files = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True).stdout.split()
        pat = re.compile(r"HEVY_KEY_ENCRYPTION_KEYS?\s*=\s*[\"']?[A-Za-z0-9_\-:,]{30,}")
        for f in files:
            p = os.path.join(REPO, f)
            if os.path.isfile(p) and not f.endswith((".png", ".db")):
                with open(p, encoding="utf-8", errors="ignore") as fh:
                    self.assertIsNone(pat.search(fh.read()), f)


if __name__ == "__main__":
    unittest.main()
