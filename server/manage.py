"""運用CLI。サーバーと同じ設定(.env.local とプロセス環境変数)・同じDBを使う。実行ディレクトリには依存しない。

  scripts\\setup.ps1                                 初回設定（鍵の生成・保存、初期管理者）。通常は直接これを使う
  py -m server.manage init                          同上
  py -m server.manage check                         設定と鍵・DBの整合性を確認（何も変更しない）
  py -m server.manage bootstrap-admin               管理者が居ない場合だけ、管理者用の招待リンクを発行（本番向け）
  py -m server.manage list-users                    利用者一覧
  py -m server.manage rotate-keys                   保存済みAPIキーを先頭の鍵で再暗号化（実行前にDBをバックアップ）
  py -m server.manage reset-hevy-keys --confirm-key-lost   鍵を紛失した場合の最終手段（Hevy連携のみ解除）
  py -m server.manage generate-key                  本番のシークレット登録用に鍵を1つ表示（起動手順では使わない）

鍵は既存の値を勝手に再生成・上書きしない。既存DBに暗号化済みキーがあるのに鍵が無い/合わない場合は、何も変更せず停止する。
"""
import argparse
import datetime
import os
import pathlib
import shutil
import sqlite3
import sys

from . import accounts, security
from .config import (ENV_FILE, KEY_VARS, MISSING_KEY_WITH_DB, ConfigError, effective_env, encrypted_rows, home_dir, load_config, parse_enc_keys,
                     parse_env_file, resolve_db_path, verify_keys_match_db)
from .db import connect, init_db

DEV_DEFAULTS = {"APP_ENV": "development", "PUBLIC_BASE_URL": "http://127.0.0.1:8000"}


def _stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def backup_db(db_path):
    """SQLiteの整合したコピーを backups/ に作る。元DBは変更しない。"""
    if not os.path.exists(db_path):
        return None
    folder = os.path.join(os.path.dirname(db_path), "backups")
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, f"{os.path.basename(db_path)}.{_stamp()}.bak")
    src = sqlite3.connect(pathlib.Path(db_path).as_uri() + "?mode=ro", uri=True, timeout=10)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def _has_admin(db_path):
    if not os.path.exists(db_path):
        return False
    try:
        conn = sqlite3.connect(pathlib.Path(db_path).as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            return conn.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone() is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _append_env(envfile, items):
    """既存ファイルは先にバックアップし、既存の項目は触らず末尾に追記するだけ。新規作成時は所有者のみ読み書き。"""
    if os.path.exists(envfile):
        shutil.copy2(envfile, f"{envfile}.bak-{_stamp()}")
        with open(envfile, "rb") as f:
            data = f.read()
        prefix = "" if (not data or data.endswith(b"\n")) else "\n"
    else:
        prefix = ""
    fd = os.open(envfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as f:
        f.write(prefix + "".join(f"{k}={v}\n" for k, v in items.items()))


def cmd_init(home, env, out, err):
    envfile = os.path.join(home, ENV_FILE)
    file_vals = parse_env_file(envfile)
    eff = effective_env(env, home)
    db_path = resolve_db_path(eff, home)
    changes = {}
    if any((eff.get(k) or "").strip() for k in KEY_VARS):
        try:
            verify_keys_match_db(db_path, parse_enc_keys(eff))
        except ConfigError as e:
            print(f"エラー: {e}\n既存の鍵・DBは変更していません。", file=err)
            return 1
        print("既存の暗号化鍵を保持しました（値は表示しません）。", file=out)
    else:
        if any(k in file_vals for k in KEY_VARS):
            print(f"エラー: {envfile} に鍵の項目はありますが値が空です。自動では上書きしません。値を設定するか、その行を削除してから再実行してください。", file=err)
            return 1
        if encrypted_rows(db_path):
            print("エラー: " + MISSING_KEY_WITH_DB.format(db=db_path, envfile=envfile), file=err)
            return 1
        changes["HEVY_KEY_ENCRYPTION_KEY"] = security.generate_key()
    for k, v in DEV_DEFAULTS.items():
        if k not in eff and k not in file_vals:
            changes[k] = v
    if changes:
        _append_env(envfile, changes)
        print(f"{envfile} に次の項目を追記しました（既存の項目は変更していません）: {', '.join(changes)}", file=out)
    try:
        cfg = load_config(env, home)
        verify_keys_match_db(cfg.db_path, cfg.enc_keys)
    except ConfigError as e:
        print(f"エラー: {e}", file=err)
        return 1
    if _has_admin(cfg.db_path):
        print("管理者は作成済みです（再作成しません）。", file=out)
        return 0
    bk = backup_db(cfg.db_path)
    if bk:
        print(f"DBをバックアップしました: {bk}", file=out)
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    try:
        token, exp = accounts.create_invite(conn, "admin", None, 24)
    finally:
        conn.close()
    base = cfg.public_base_url or "http://127.0.0.1:8000"
    print(f"管理者用の招待リンク（一回限り・{exp} まで有効）:\n{base}/#invite={token}\nこのリンクを開き、管理者のユーザー名とパスワードを自分で設定してください。", file=out)
    return 0


def main(argv=None, out=None, err=None, env=None, home=None):
    out, err = out or sys.stdout, err or sys.stderr
    home = home or home_dir()
    ap = argparse.ArgumentParser(description="kcal-note server management")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("generate-key", "init", "check", "bootstrap-admin", "list-users", "rotate-keys"):
        sub.add_parser(name)
    r = sub.add_parser("reset-hevy-keys")
    r.add_argument("--confirm-key-lost", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "generate-key":
        print("警告: 本番のシークレット登録用です。起動のたびに実行しないでください（鍵が変わると保存済みAPIキーを復号できません）。", file=err)
        print(security.generate_key(), file=out)
        return 0
    if args.cmd == "init":
        return cmd_init(home, env, out, err)
    if args.cmd == "reset-hevy-keys":
        if not args.confirm_key_lost:
            print("鍵を紛失した場合の最終手段です。--confirm-key-lost を付けて実行してください（実行前にDBを自動バックアップし、Hevy連携の登録だけを削除します）。", file=err)
            return 2
        db_path = resolve_db_path(effective_env(env, home), home)
        if not os.path.exists(db_path):
            print("DBが見つかりません。", file=err)
            return 1
        print(f"DBをバックアップしました: {backup_db(db_path)}", file=out)
        conn = connect(db_path)
        n = conn.execute("DELETE FROM hevy_connections").rowcount
        print(f"Hevy連携の登録 {n}件を削除しました。利用者・招待は残っています。各利用者がAPIキーを登録し直してください。", file=out)
        conn.close()
        return 0
    try:
        cfg = load_config(env, home)
        verify_keys_match_db(cfg.db_path, cfg.enc_keys)
    except ConfigError as e:
        print(f"設定エラー: {e}", file=err)
        return 2
    if args.cmd == "check":
        print(f"設定OK（鍵の値は表示しません）。DB: {cfg.db_path}", file=out)
        return 0
    if args.cmd == "list-users":
        if not os.path.exists(cfg.db_path):
            print("DBがまだありません。", file=err)
            return 1
        conn = connect(cfg.db_path)
        for row in conn.execute("SELECT id, username, role, status FROM users ORDER BY id"):
            print(f"{row['id']}\t{row['username']}\t{row['role']}\t{row['status']}", file=out)
        conn.close()
        return 0
    if args.cmd == "bootstrap-admin":
        if _has_admin(cfg.db_path):
            print("管理者は既に存在します。追加の利用者は管理者画面から招待してください。", file=err)
            return 1
        print(f"DBをバックアップしました: {backup_db(cfg.db_path)}" if os.path.exists(cfg.db_path) else "新規DBを作成します。", file=out)
        init_db(cfg.db_path)
        conn = connect(cfg.db_path)
        token, exp = accounts.create_invite(conn, "admin", None, 24)
        conn.close()
        print(f"管理者用の招待リンク（一回限り・{exp} まで有効）:\n{cfg.public_base_url or 'http://127.0.0.1:8000'}/#invite={token}", file=out)
        return 0
    if args.cmd == "rotate-keys":
        if not os.path.exists(cfg.db_path):
            print("DBがありません。", file=err)
            return 1
        print(f"DBをバックアップしました: {backup_db(cfg.db_path)}", file=out)
        ring, n = security.KeyRing(cfg.enc_keys), 0
        conn = connect(cfg.db_path)
        for row in conn.execute("SELECT user_id, key_enc FROM hevy_connections").fetchall():
            if ring.needs_rotation(row["key_enc"]):
                plain = ring.decrypt(row["key_enc"], "hevy-api-key", row["user_id"])
                conn.execute("UPDATE hevy_connections SET key_enc=? WHERE user_id=?", (ring.encrypt(plain, "hevy-api-key", row["user_id"]), row["user_id"]))
                n += 1
        conn.close()
        print(f"{n}件を再暗号化しました。", file=out)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
