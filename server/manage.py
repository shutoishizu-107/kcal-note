"""運用コマンド。

  py -m server.manage generate-key                 暗号化鍵を1つ生成（環境変数 HEVY_KEY_ENCRYPTION_KEY に設定）
  py -m server.manage bootstrap-admin              初期管理者用の招待リンクを発行（管理者がまだ居ない時だけ）
  py -m server.manage list-users                   利用者一覧
  py -m server.manage rotate-keys                  保存済みAPIキーを現行の暗号化鍵で再暗号化
パスワードは作成せず、招待リンクを開いた本人が自分で設定する（既定パスワードは存在しない）。
"""
import argparse
import sys

from . import accounts, security
from .config import ConfigError, load_config
from .db import connect, init_db


def main(argv=None, out=sys.stdout):
    ap = argparse.ArgumentParser(description="kcal-note server management")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate-key")
    b = sub.add_parser("bootstrap-admin")
    b.add_argument("--ttl-hours", type=int, default=24)
    sub.add_parser("list-users")
    sub.add_parser("rotate-keys")
    args = ap.parse_args(argv)

    if args.cmd == "generate-key":
        print(security.generate_key(), file=out)
        return 0
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"設定エラー: {e}", file=sys.stderr)
        return 2
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    if args.cmd == "bootstrap-admin":
        if accounts.admin_exists(conn):
            print("管理者は既に存在します。追加の利用者は管理者画面から招待してください。", file=sys.stderr)
            return 1
        token, exp = accounts.create_invite(conn, "admin", None, args.ttl_hours)
        base = cfg.public_base_url or "http://127.0.0.1:8000"
        print(f"管理者用の招待リンク（一回限り・{exp} まで有効）:\n{base}/#invite={token}", file=out)
        return 0
    if args.cmd == "list-users":
        for r in conn.execute("SELECT id, username, role, status FROM users ORDER BY id"):
            print(f"{r['id']}\t{r['username']}\t{r['role']}\t{r['status']}", file=out)
        return 0
    if args.cmd == "rotate-keys":
        ring, n = security.KeyRing(cfg.enc_keys), 0
        for r in conn.execute("SELECT user_id, key_enc FROM hevy_connections").fetchall():
            if ring.needs_rotation(r["key_enc"]):
                plain = ring.decrypt(r["key_enc"], "hevy-api-key", r["user_id"])
                conn.execute("UPDATE hevy_connections SET key_enc=? WHERE user_id=?", (ring.encrypt(plain, "hevy-api-key", r["user_id"]), r["user_id"]))
                n += 1
        print(f"{n}件を再暗号化しました。", file=out)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
