"""ローカル動作確認用: 架空のHevy APIサーバーとアプリサーバーを同時に起動する（本番では使わない）。

  .venv\\Scripts\\python -m server.dev_e2e

起動後に表示されるURLを開き、表示される招待リンクで管理者アカウントを作る。
架空のHevyアカウントのAPIキー: 11111111-1111-4111-8111-111111111111
制御(GET): /add?id=W9&days=0&title=...  /delete?id=W9  /fail?code=429&n=1
"""
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import accounts, security  # noqa: E402
from server.app import create_app  # noqa: E402
from server.config import Config  # noqa: E402
from server.db import connect, init_db  # noqa: E402
from server.sync import iso, utcnow  # noqa: E402
from server.test_server import FakeHevy, KEY_A, workout  # noqa: E402


def main():
    fake = FakeHevy()
    base = fake.start()
    now = utcnow()
    for i, (d, t) in enumerate([(0, "Fictional Push Day"), (2, "Fictional Pull Day"), (5, "Fictional Leg Day")]):
        w = workout(f"E2E-{i+1}", d, title=t, now=now)
        fake.add("TEST-ACC-A", w)

    class Control(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/add":
                w = workout(q["id"], float(q.get("days", 0)), title=q.get("title", "Fictional Added"), updated=iso(utcnow()))
                fake.add("TEST-ACC-A", w)
            elif u.path == "/delete":
                fake.delete("TEST-ACC-A", q["id"])
            elif u.path == "/fail":
                fake.fail_plan = [int(q["code"])] * int(q.get("n", 1))
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")

    ctl = HTTPServer(("127.0.0.1", 0), Control)
    threading.Thread(target=ctl.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp()
    key = security.generate_key()
    import base64
    cfg = Config(db_path=os.path.join(tmp, "e2e.db"), enc_keys=[("k1", base64.urlsafe_b64decode(key + "=" * (-len(key) % 4)))], public_base_url="http://127.0.0.1:8765",
                 cookie_secure=False, session_ttl_days=1, hevy_base=base, app_env="test", page_delay=0, min_sync_interval=2,
                 static_root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    app = create_app(cfg, sleep=lambda s: None)
    conn = connect(cfg.db_path)
    token, _ = accounts.create_invite(conn, "admin", None, 24)
    print(f"fake hevy: {base}\ncontrol:   http://127.0.0.1:{ctl.server_port}\nadmin invite: http://127.0.0.1:8765/#invite={token}", flush=True)
    app.run(host="127.0.0.1", port=8765, threaded=True)


if __name__ == "__main__":
    main()
