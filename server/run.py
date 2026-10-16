"""開発用の通常起動。scripts\\start.ps1 から呼ばれる。鍵の生成や管理者の作成は行わない（初回設定は scripts\\setup.ps1）。
本番は gunicorn で server.wsgi:app を起動する（Dockerfile 参照）。"""
import os
import sys

from .app import create_app
from .config import ConfigError, load_config


def main():
    try:
        cfg = load_config()
        app = create_app(cfg)
    except ConfigError as e:
        print(f"起動を中止しました: {e}", file=sys.stderr)
        return 2
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8000"))
    print(f"起動します: http://{host}:{port}/  （停止は Ctrl+C。DB: {cfg.db_path}）", flush=True)
    app.run(host=host, port=port, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
