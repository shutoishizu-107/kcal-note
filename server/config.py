"""環境変数からの設定読み込み。秘密情報はここ（環境変数）だけで保持し、DBやGitには置かない。"""
import base64
import os
import pathlib
import sqlite3
import urllib.parse
from dataclasses import dataclass

DEFAULT_HEVY_BASE = "https://api.hevyapp.com"
CODE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = ".env.local"
KEY_VARS = ("HEVY_KEY_ENCRYPTION_KEYS", "HEVY_KEY_ENCRYPTION_KEY")


def home_dir():
    """設定(.env.local)とDBの基準ディレクトリ。実行ディレクトリに依存しない（KCAL_HOME で変更可）。"""
    return os.path.abspath(os.environ.get("KCAL_HOME") or CODE_ROOT)


class ConfigError(Exception):
    pass


def parse_env_file(path):
    """KEY=VALUE 形式のファイルを読む。存在しなければ空。値は表示しない。"""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[k.strip()] = v
    except FileNotFoundError:
        pass
    return out


def effective_env(env=None, home=None):
    """.env.local を基礎にし、空でないプロセス環境変数で上書き（プロセス環境変数を優先）。"""
    home = home or home_dir()
    merged = parse_env_file(os.path.join(home, ENV_FILE))
    for k, v in (os.environ if env is None else env).items():
        if v != "":
            merged[k] = v
    return merged


def resolve_db_path(env, home):
    p = env.get("DATABASE_PATH") or os.path.join("data", "app.db")
    return os.path.abspath(p if os.path.isabs(p) else os.path.join(home, p))


def _open_ro(path):
    return sqlite3.connect(pathlib.Path(path).as_uri() + "?mode=ro", uri=True, timeout=10)


def encrypted_rows(db_path):
    """既存DBに保存済みの暗号化APIキー [(user_id, blob)]。DBを変更しない（読み取り専用）。"""
    if not os.path.exists(db_path):
        return []
    try:
        conn = _open_ro(db_path)
        try:
            return conn.execute("SELECT user_id, key_enc FROM hevy_connections").fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return []
        raise ConfigError("既存DBを読み取れませんでした。DBは変更していません。他のプロセスが使用中でないか、パスを確認してください。")
    except sqlite3.DatabaseError:
        raise ConfigError("既存DBが壊れている可能性があります。DBは変更せず停止しました。バックアップから復元してください。")


MISSING_KEY_WITH_DB = (
    "暗号化鍵(HEVY_KEY_ENCRYPTION_KEY)が見つかりません。既存のDB({db})には暗号化済みのHevy APIキーが保存されているため、"
    "鍵を新規生成すると復号できなくなります。DBや鍵は変更せず起動を中止しました。\n"
    "復旧方法: (1) 以前の鍵を、環境変数または {envfile} の HEVY_KEY_ENCRYPTION_KEY に設定する（本番はホスティングのシークレット）。"
    "(2) 鍵を紛失している場合は、保存済みAPIキーは復元できません。ユーザー・招待を残したまま、"
    "`python -m server.manage reset-hevy-keys --confirm-key-lost` でHevy連携だけを解除（実行前にDBを自動バックアップ）し、各利用者がキーを登録し直してください。"
)


def parse_enc_keys(env):
    raw = (env.get("HEVY_KEY_ENCRYPTION_KEYS") or "").strip()
    if not raw and (env.get("HEVY_KEY_ENCRYPTION_KEY") or "").strip():
        raw = "k1:" + env["HEVY_KEY_ENCRYPTION_KEY"].strip()
    if not raw:
        raise ConfigError("HEVY_KEY_ENCRYPTION_KEY が未設定です。初回設定をまだ行っていない場合は scripts\\setup.ps1 を実行してください（本番はホスティングのシークレットに設定）。")
    keys, seen = [], set()
    for part in raw.split(","):
        kid, sep, b64 = part.strip().partition(":")
        if not sep or not kid or kid in seen or ":" in kid:
            raise ConfigError("HEVY_KEY_ENCRYPTION_KEYS は 'kid:base64鍵,kid2:base64鍵' の形式で指定してください（鍵の値は表示しません）")
        seen.add(kid)
        keys.append((kid, _b64key(b64)))
    return keys


def verify_keys_match_db(db_path, enc_keys):
    """既存の暗号化データを現在の鍵で復号できるか確認する。不一致なら起動を止める（DB・鍵は変更しない）。"""
    from . import security
    rows = encrypted_rows(db_path)
    ring, bad = security.KeyRing(enc_keys), 0
    for user_id, blob in rows:
        try:
            ring.decrypt(blob, "hevy-api-key", user_id)
        except Exception:
            bad += 1
    if bad:
        raise ConfigError(
            f"設定されている暗号化鍵で、既存DBの保存済みAPIキー {bad}件を復号できません（鍵が以前と異なります）。DBと鍵は変更せず起動を中止しました。\n"
            "復旧方法: 以前使っていた鍵を設定し直す（鍵を意図して変えた場合は 'HEVY_KEY_ENCRYPTION_KEYS=新kid:新鍵,旧kid:旧鍵' の形で両方を指定し、"
            "`python -m server.manage rotate-keys` で再暗号化してから旧鍵を外す）。鍵を紛失した場合は `python -m server.manage reset-hevy-keys --confirm-key-lost`。")


@dataclass
class Config:
    db_path: str
    enc_keys: list  # [(kid, 32byte key)] 先頭が暗号化に使う現行鍵。残りは復号専用（鍵ローテーション用）
    public_base_url: str
    cookie_secure: bool
    session_ttl_days: int
    hevy_base: str
    app_env: str
    page_delay: float
    min_sync_interval: int
    static_root: str


def _b64key(text):
    try:
        key = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except Exception:
        raise ConfigError("暗号化鍵は base64 文字列で指定してください")
    if len(key) != 32:
        raise ConfigError("暗号化鍵は32バイト（base64）である必要があります")
    return key


def load_config(env=None, home=None):
    home = home or home_dir()
    env = effective_env(env, home)
    db_path = resolve_db_path(env, home)
    app_env = env.get("APP_ENV", "production").strip().lower()
    dev = app_env in ("development", "test")
    hevy_base = env.get("HEVY_API_BASE", DEFAULT_HEVY_BASE).rstrip("/")
    p = urllib.parse.urlparse(hevy_base)
    local = p.hostname in ("localhost", "127.0.0.1")
    if p.scheme != "https" and not (p.scheme == "http" and local and dev):
        raise ConfigError("HEVY_API_BASE は https である必要があります（開発/テスト時のlocalhostを除く）")
    base_url = env.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if base_url and not dev and not base_url.startswith("https://"):
        raise ConfigError("PUBLIC_BASE_URL は https:// で始まる必要があります")
    try:
        enc_keys = parse_enc_keys(env)
    except ConfigError:
        if not any((env.get(k) or "").strip() for k in KEY_VARS) and encrypted_rows(db_path):
            raise ConfigError(MISSING_KEY_WITH_DB.format(db=db_path, envfile=os.path.join(home, ENV_FILE)))
        raise
    secure_default = "0" if dev else "1"
    return Config(
        db_path=db_path,
        enc_keys=enc_keys,
        public_base_url=base_url,
        cookie_secure=env.get("COOKIE_SECURE", secure_default) not in ("0", "false", "False"),
        session_ttl_days=int(env.get("SESSION_TTL_DAYS", "14")),
        hevy_base=hevy_base,
        app_env=app_env,
        page_delay=float(env.get("HEVY_PAGE_DELAY", "0.25")),
        min_sync_interval=int(env.get("MIN_SYNC_INTERVAL_SECONDS", "10")),
        static_root=env.get("STATIC_ROOT", CODE_ROOT),
    )
