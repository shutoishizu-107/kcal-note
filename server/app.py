"""招待制の認証つきバックエンド。HTMLと同じオリジンで配信し、HevyのAPIキー管理と通信はここだけで行う。"""
import datetime
import hashlib
import hmac
import json
import logging
import os
import time
from urllib.parse import urlparse

from flask import Flask, Response, g, jsonify, request, send_from_directory

from . import accounts, security
from .config import load_config
from .db import connect, init_db
from .hevy_client import HevyClient, HevyError
from .sync import SyncError, get_state, iso, normalize_user, parse_iso, run_sync, utcnow

log = logging.getLogger("kcal")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
PUBLIC_POST = ("login", "invite_accept")
STATIC_FILES = ("index.html", "sw.js", "manifest.webmanifest")


def account_key(account_id):
    return hashlib.sha256(("hevy-account:" + account_id).encode()).hexdigest()[:16]


def create_app(config=None, sleep=time.sleep):
    cfg = config or load_config()
    ring = security.KeyRing(cfg.enc_keys)
    init_db(cfg.db_path)
    app = Flask(__name__, static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=64 * 1024, JSON_SORT_KEYS=False)
    if os.environ.get("TRUST_PROXY", "0") == "1":
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    cookie_name = "__Host-kcal" if cfg.cookie_secure else "kcal"

    def db():
        if "db" not in g:
            g.db = connect(cfg.db_path)
        return g.db

    @app.teardown_appcontext
    def close_db(_):
        c = g.pop("db", None)
        if c:
            c.close()

    def fail(code, message, status=400, **extra):
        return jsonify({"error": code, "message": message, **extra}), status

    def body():
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    def rate_ok(bucket, limit, window):
        c, now = db(), utcnow()
        c.execute("DELETE FROM rate_events WHERE at<?", (iso(now - datetime.timedelta(hours=1)),))
        n = c.execute("SELECT COUNT(*) FROM rate_events WHERE bucket=? AND at>?", (bucket, iso(now - datetime.timedelta(seconds=window)))).fetchone()[0]
        if n >= limit:
            return False
        c.execute("INSERT INTO rate_events(bucket, at) VALUES(?,?)", (bucket, iso(now)))
        return True

    # ---- 共通の前処理: Origin検証、セッション、CSRF ----
    @app.before_request
    def guard():
        g.user = None
        if not request.path.startswith("/api/"):
            return None
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("Origin")
            if origin:
                if urlparse(origin).netloc != request.host:
                    return fail("bad_origin", "不正なリクエスト元です", 403)
            elif request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
                return fail("bad_origin", "不正なリクエスト元です", 403)
        row = accounts.find_session(db(), request.cookies.get(cookie_name))
        g.user = row
        public = request.endpoint in PUBLIC_POST or request.endpoint in ("session", "healthz")
        if row is None and not public:
            return fail("unauthorized", "ログインが必要です", 401)
        if row is not None and request.method not in SAFE_METHODS and request.endpoint not in PUBLIC_POST:
            if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), row["csrf_token"]):
                return fail("csrf", "セッションが無効です。ページを再読み込みしてください", 403)
        return None

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        if cfg.cookie_secure:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(Exception)
    def on_error(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return fail("http_" + str(e.code), e.name, e.code)
        log.error("unhandled %s", type(e).__name__)  # 本文やキーを含みうる内容はログに出さない
        return fail("internal", "サーバーエラーが発生しました", 500)

    def set_session(resp, token):
        resp.set_cookie(cookie_name, token, max_age=cfg.session_ttl_days * 86400, httponly=True, secure=cfg.cookie_secure, samesite="Lax", path="/")
        return resp

    def user_json(u):
        return {"id": u["id"], "username": u["username"], "role": u["role"]}

    # ---- 静的ファイル（許可リストのみ）----
    @app.get("/")
    def index():
        r = send_from_directory(cfg.static_root, "index.html")
        r.headers["Cache-Control"] = "no-cache"
        return r

    @app.get("/<path:name>")
    def static_file(name):
        if name in STATIC_FILES or (name.startswith("icons/") and name.endswith(".png") and ".." not in name):
            r = send_from_directory(cfg.static_root, name)
            r.headers["Cache-Control"] = "no-cache"
            return r
        return fail("not_found", "見つかりません", 404)

    @app.get("/api/healthz")
    def healthz():
        return jsonify({"ok": True})

    # ---- 認証 ----
    @app.get("/api/session")
    def session():
        u = g.user
        return jsonify({"backend": True, "user": user_json(u) if u else None, "csrfToken": u["csrf_token"] if u else None})

    @app.post("/api/login")
    def login():
        d, ip = body(), request.remote_addr or "?"
        username, password = d.get("username"), d.get("password")
        if not isinstance(username, str) or not isinstance(password, str) or not username or len(password) > 200:
            return fail("invalid_credentials", "ユーザー名またはパスワードが違います", 401)
        c, now = db(), utcnow()
        since = iso(now - datetime.timedelta(minutes=15))
        by_user = c.execute("SELECT COUNT(*) FROM login_attempts WHERE ip=? AND username=? AND at>?", (ip, username.lower(), since)).fetchone()[0]
        by_ip = c.execute("SELECT COUNT(*) FROM login_attempts WHERE ip=? AND at>?", (ip, since)).fetchone()[0]
        if by_user >= 5 or by_ip >= 30:
            return fail("too_many_attempts", "試行回数が多すぎます。しばらくしてから再試行してください", 429)
        u = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        ok = security.verify_password(password, u["password_hash"] if u else security.DUMMY_HASH)
        if not (u and ok and u["status"] == "active"):
            c.execute("INSERT INTO login_attempts(ip, username, at) VALUES(?,?,?)", (ip, username.lower(), iso(now)))
            return fail("invalid_credentials", "ユーザー名またはパスワードが違います", 401)
        token = accounts.create_session(c, u["id"], cfg.session_ttl_days)
        return set_session(jsonify({"user": user_json(u)}), token)

    @app.post("/api/logout")
    def logout():
        db().execute("DELETE FROM sessions WHERE id=?", (g.user["sid"],))
        resp = jsonify({"ok": True})
        resp.delete_cookie(cookie_name, path="/")
        return resp

    @app.post("/api/invite/accept")
    def invite_accept():
        d = body()
        if not rate_ok("invite:" + (request.remote_addr or "?"), 10, 600):
            return fail("too_many_attempts", "試行回数が多すぎます", 429)
        try:
            uid = accounts.accept_invite(db(), d.get("token"), d.get("username"), d.get("password"))
        except accounts.AccountError as e:
            return fail(e.code, e.message, 400)
        token = accounts.create_session(db(), uid, cfg.session_ttl_days)
        u = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return set_session(jsonify({"user": user_json(u)}), token)

    @app.post("/api/account/password")
    def change_password():
        d, u = body(), g.user
        row = db().execute("SELECT password_hash FROM users WHERE id=?", (u["id"],)).fetchone()
        if not isinstance(d.get("current"), str) or not security.verify_password(d["current"], row["password_hash"]):
            return fail("invalid_credentials", "現在のパスワードが違います", 400)
        problem = security.check_password_policy(d.get("new"))
        if problem:
            return fail("weak_password", problem, 400)
        db().execute("UPDATE users SET password_hash=? WHERE id=?", (security.hash_password(d["new"]), u["id"]))
        db().execute("DELETE FROM sessions WHERE user_id=? AND id<>?", (u["id"], u["sid"]))
        return jsonify({"ok": True})

    # ---- 管理者 ----
    def admin_only():
        return None if g.user["role"] == "admin" else fail("forbidden", "管理者のみ実行できます", 403)

    @app.get("/api/admin/users")
    def admin_users():
        denied = admin_only()
        if denied:
            return denied
        users = [dict(r) for r in db().execute("SELECT id, username, role, status, created_at FROM users ORDER BY id")]
        invites = [dict(r) for r in db().execute("SELECT id, role, created_at, expires_at, used_at FROM invites ORDER BY id DESC LIMIT 50")]
        return jsonify({"users": users, "invites": invites})

    @app.post("/api/admin/invites")
    def admin_invite():
        denied = admin_only()
        if denied:
            return denied
        ttl = body().get("ttlHours", 72)
        token, exp = accounts.create_invite(db(), "user", g.user["id"], ttl if isinstance(ttl, int) else 72)
        base = cfg.public_base_url or request.host_url.rstrip("/")
        return jsonify({"inviteUrl": f"{base}/#invite={token}", "expiresAt": exp})

    @app.delete("/api/admin/invites/<int:invite_id>")
    def admin_invite_revoke(invite_id):
        denied = admin_only()
        if denied:
            return denied
        db().execute("UPDATE invites SET expires_at=? WHERE id=? AND used_at IS NULL", (iso(utcnow() - datetime.timedelta(seconds=1)), invite_id))
        return jsonify({"ok": True})

    @app.post("/api/admin/users/<int:user_id>/<action>")
    def admin_user_action(user_id, action):
        denied = admin_only()
        if denied:
            return denied
        if action not in ("suspend", "activate"):
            return fail("not_found", "見つかりません", 404)
        target = db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            return fail("not_found", "利用者が見つかりません", 404)
        if action == "suspend":
            if target["id"] == g.user["id"]:
                return fail("invalid", "自分自身は停止できません", 400)
            db().execute("UPDATE users SET status='suspended' WHERE id=?", (user_id,))
            db().execute("DELETE FROM sessions WHERE user_id=?", (user_id,))  # 停止後は同期・取得を一切許可しない
        else:
            db().execute("UPDATE users SET status='active' WHERE id=?", (user_id,))
        return jsonify({"ok": True})

    # ---- Hevy連携 ----
    def hevy_error(e):
        status = {"auth": 400, "rate": 429, "server": 502, "network": 502, "bad": 502}.get(e.kind, 502)
        return fail("hevy_" + e.kind, str(e), status, retryAfter=e.retry_after)

    def conn_row():
        return db().execute("SELECT * FROM hevy_connections WHERE user_id=?", (g.user["id"],)).fetchone()

    def status_json():
        c = conn_row()
        if not c:
            return {"connected": False}
        st = get_state(db(), g.user["id"], c["hevy_account_id"])
        return {"connected": True, "keyHint": c["key_hint"], "account": {"username": c["hevy_username"], "name": c["hevy_name"]},
                "accountKey": account_key(c["hevy_account_id"]), "generation": st["generation"], "autoSync": bool(c["auto_sync"]),
                "initialDays": c["initial_days"], "initialDone": bool(st["initial_done"]), "initialFrom": st["initial_from"],
                "lastSuccessAt": st["last_success_at"], "lastAttemptAt": st["last_attempt_at"], "lastError": st["last_error"],
                "templatesAt": st["templates_at"]}

    def check_key(raw):
        key = security.valid_hevy_key(raw)
        if not key:
            return None, None, fail("invalid_key_format", "APIキーの形式が正しくありません（UUID形式）", 400)
        if not rate_ok(f"hevykey:{g.user['id']}", 10, 60):
            return None, None, fail("too_many_attempts", "接続テストの回数が多すぎます。1分ほど待ってください", 429)
        try:
            info = normalize_user(HevyClient(cfg.hevy_base, key, sleep=sleep, max_attempts=2).get("/v1/user/info"))
        except HevyError as e:
            return None, None, hevy_error(e)
        return key, info, None

    @app.get("/api/hevy/status")
    def hevy_status():
        return jsonify(status_json())

    @app.post("/api/hevy/test")
    def hevy_test():
        key, info, err = check_key(body().get("apiKey"))
        if err:
            return err
        c = conn_row()
        return jsonify({"ok": True, "account": {"username": info["username"], "name": info["name"], "isPro": info["isPro"]},
                        "sameAsSaved": (c["hevy_account_id"] == info["id"]) if c else None})

    def purge_account(user_id, account_id):
        for t in ("hevy_state", "hevy_workouts", "hevy_changes", "hevy_templates"):
            db().execute(f"DELETE FROM {t} WHERE user_id=? AND account_id=?", (user_id, account_id))

    @app.post("/api/hevy/connect")
    def hevy_connect():
        d = body()
        key, info, err = check_key(d.get("apiKey"))
        if err:
            return err
        uid, old = g.user["id"], conn_row()
        if old and old["hevy_account_id"] != info["id"]:
            choice = d.get("accountChange")
            if choice not in ("purge", "keep"):
                return fail("account_changed", "別のHevyアカウントのキーです。履歴を混在させないため、扱いを選択してください", 409,
                            current={"username": old["hevy_username"], "name": old["hevy_name"]}, incoming={"username": info["username"], "name": info["name"]})
            if choice == "purge":
                purge_account(uid, old["hevy_account_id"])
        days = d.get("initialDays", old["initial_days"] if old else 90)
        days = max(7, min(days, 3650)) if isinstance(days, int) else 90
        now = iso(utcnow())
        enc = ring.encrypt(key, "hevy-api-key", uid)
        db().execute(
            "INSERT INTO hevy_connections(user_id, key_enc, key_hint, hevy_account_id, hevy_username, hevy_name, auto_sync, initial_days, created_at, updated_at) VALUES(?,?,?,?,?,?,1,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET key_enc=excluded.key_enc, key_hint=excluded.key_hint, hevy_account_id=excluded.hevy_account_id, hevy_username=excluded.hevy_username, hevy_name=excluded.hevy_name, updated_at=excluded.updated_at",
            (uid, enc, key[-4:], info["id"], info["username"], info["name"], days, now, now))
        st = get_state(db(), uid, info["id"])
        if not st["initial_done"]:
            db().execute("UPDATE hevy_connections SET initial_days=? WHERE user_id=?", (days, uid))
        return jsonify(status_json())

    @app.put("/api/hevy/settings")
    def hevy_settings():
        d, c = body(), conn_row()
        if not c:
            return fail("not_connected", "Hevy連携が設定されていません", 409)
        if isinstance(d.get("autoSync"), bool):
            db().execute("UPDATE hevy_connections SET auto_sync=? WHERE user_id=?", (1 if d["autoSync"] else 0, g.user["id"]))
        st = get_state(db(), g.user["id"], c["hevy_account_id"])
        if isinstance(d.get("initialDays"), int) and not st["initial_done"]:
            db().execute("UPDATE hevy_connections SET initial_days=? WHERE user_id=?", (max(7, min(d["initialDays"], 3650)), g.user["id"]))
        return jsonify(status_json())

    @app.post("/api/hevy/disconnect")
    def hevy_disconnect():
        d, c = body(), conn_row()
        if not c:
            return jsonify({"ok": True, "deletedHistory": False})
        delete = d.get("deleteHistory") is True
        db().execute("DELETE FROM hevy_connections WHERE user_id=?", (g.user["id"],))  # キーを削除し自動同期も停止
        if delete:
            purge_account(g.user["id"], c["hevy_account_id"])
        return jsonify({"ok": True, "deletedHistory": delete, "accountKey": account_key(c["hevy_account_id"]),
                        "note": "アプリからキーを削除しました。Hevy側でのキーの失効は、Hevyの設定画面で別途行ってください。"})

    @app.post("/api/hevy/sync")
    def hevy_sync():
        d = body()
        mode = d.get("mode", "incremental")
        if mode not in ("incremental", "backfill"):
            return fail("invalid", "mode が不正です", 400)
        try:
            summary = run_sync(db(), cfg, ring, g.user["id"], mode=mode, days=d.get("days") if isinstance(d.get("days"), int) else None,
                               sleep=sleep, force_templates=d.get("refreshTemplates") is True)
        except SyncError as e:
            status = {"too_soon": 429, "busy": 409, "not_connected": 409, "hevy_auth": 400, "hevy_rate": 429}.get(e.code, 502)
            return fail(e.code, e.message, status, retryAfter=e.retry_after)
        return jsonify({"ok": True, "summary": summary, "status": status_json()})

    @app.get("/api/hevy/changes")
    def hevy_changes():
        c = conn_row()
        if not c:
            return fail("not_connected", "Hevy連携が設定されていません", 409)
        uid, acc = g.user["id"], c["hevy_account_id"]
        st = get_state(db(), uid, acc)
        gen = request.args.get("generation", type=int)
        after = max(request.args.get("after", 0, type=int), 0)
        base = {"accountKey": account_key(acc), "generation": st["generation"]}
        if gen is not None and gen != st["generation"]:
            return jsonify({**base, "generationChanged": True, "changes": [], "nextSeq": 0, "hasMore": False})
        rows = db().execute(
            "SELECT workout_id, MAX(id) AS mid FROM hevy_changes WHERE user_id=? AND account_id=? AND id>? GROUP BY workout_id ORDER BY mid LIMIT 100", (uid, acc, after)).fetchall()
        out = []
        for r in rows:
            w = db().execute("SELECT payload, deleted FROM hevy_workouts WHERE user_id=? AND account_id=? AND workout_id=?", (uid, acc, r["workout_id"])).fetchone()
            if w and not w["deleted"] and w["payload"]:
                out.append({"seq": r["mid"], "kind": "upsert", "workoutId": r["workout_id"], "workout": json.loads(w["payload"])})
            else:
                out.append({"seq": r["mid"], "kind": "delete", "workoutId": r["workout_id"]})
        next_seq = rows[-1]["mid"] if rows else after
        more = db().execute("SELECT 1 FROM hevy_changes WHERE user_id=? AND account_id=? AND id>? LIMIT 1", (uid, acc, next_seq)).fetchone() is not None
        return jsonify({**base, "generationChanged": False, "changes": out, "nextSeq": next_seq, "hasMore": more})

    @app.get("/api/hevy/templates")
    def hevy_templates():
        c = conn_row()
        if not c:
            return fail("not_connected", "Hevy連携が設定されていません", 409)
        rows = db().execute("SELECT payload FROM hevy_templates WHERE user_id=? AND account_id=?", (g.user["id"], c["hevy_account_id"])).fetchall()
        items = []
        for r in rows:
            t = json.loads(r["payload"])
            t.pop("thumbnail_url", None)  # 画像等のコピーは行わない
            items.append(t)
        st = get_state(db(), g.user["id"], c["hevy_account_id"])
        return jsonify({"accountKey": account_key(c["hevy_account_id"]), "fetchedAt": st["templates_at"], "templates": items})

    return app
