"""利用者・招待・セッションの操作（アプリとCLIで共用）。"""
import datetime

from . import security
from .sync import iso, parse_iso, utcnow


class AccountError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def admin_exists(conn):
    return conn.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone() is not None


def create_invite(conn, role, created_by, ttl_hours=72):
    ttl_hours = max(1, min(int(ttl_hours), 24 * 14))
    token = security.new_token()
    now = utcnow()
    conn.execute("INSERT INTO invites(token_hash, role, created_by, created_at, expires_at) VALUES(?,?,?,?,?)",
                 (security.token_hash(token), role, created_by, iso(now), iso(now + datetime.timedelta(hours=ttl_hours))))
    return token, iso(now + datetime.timedelta(hours=ttl_hours))


def accept_invite(conn, token, username, password):
    if not isinstance(token, str) or not token:
        raise AccountError("invalid_invite", "招待リンクが無効です")
    if not isinstance(username, str) or not security.USERNAME_RE.match(username):
        raise AccountError("invalid_username", "ユーザー名は英数字・_・.・- の3〜32文字にしてください")
    problem = security.check_password_policy(password)
    if problem:
        raise AccountError("weak_password", problem)
    now = iso(utcnow())
    conn.execute("BEGIN IMMEDIATE")
    try:
        inv = conn.execute("SELECT id, role FROM invites WHERE token_hash=? AND used_at IS NULL AND expires_at>?", (security.token_hash(token), now)).fetchone()
        if not inv:
            raise AccountError("invalid_invite", "招待リンクが無効か、期限切れ・使用済みです")
        if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise AccountError("username_taken", "このユーザー名は使えません")
        cur = conn.execute("INSERT INTO users(username, password_hash, role, status, created_at, created_by) VALUES(?,?,?,'active',?, (SELECT created_by FROM invites WHERE id=?))",
                           (username, security.hash_password(password), inv["role"], now, inv["id"]))
        uid = cur.lastrowid
        done = conn.execute("UPDATE invites SET used_at=?, used_by=? WHERE id=? AND used_at IS NULL", (now, uid, inv["id"]))
        if done.rowcount != 1:
            raise AccountError("invalid_invite", "招待リンクが無効です")
        conn.execute("COMMIT")
        return uid
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def create_session(conn, user_id, ttl_days):
    token, csrf = security.new_token(), security.new_token()
    now = utcnow()
    conn.execute("INSERT INTO sessions(token_hash, user_id, csrf_token, created_at, expires_at) VALUES(?,?,?,?,?)",
                 (security.token_hash(token), user_id, csrf, iso(now), iso(now + datetime.timedelta(days=ttl_days))))
    return token


def find_session(conn, token):
    if not token:
        return None
    return conn.execute(
        "SELECT s.id AS sid, s.csrf_token, u.id, u.username, u.role FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.token_hash=? AND s.expires_at>? AND u.status='active'", (security.token_hash(token), iso(utcnow()))).fetchone()
