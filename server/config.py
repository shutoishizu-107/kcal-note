"""環境変数からの設定読み込み。秘密情報はここ（環境変数）だけで保持し、DBやGitには置かない。"""
import base64
import os
import urllib.parse
from dataclasses import dataclass

DEFAULT_HEVY_BASE = "https://api.hevyapp.com"


class ConfigError(Exception):
    pass


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


def parse_enc_keys(env):
    raw = env.get("HEVY_KEY_ENCRYPTION_KEYS", "").strip()
    if not raw and env.get("HEVY_KEY_ENCRYPTION_KEY", "").strip():
        raw = "k1:" + env["HEVY_KEY_ENCRYPTION_KEY"].strip()
    if not raw:
        raise ConfigError("HEVY_KEY_ENCRYPTION_KEY が未設定です。`python -m server.manage generate-key` で生成して環境変数に設定してください。")
    keys, seen = [], set()
    for part in raw.split(","):
        kid, sep, b64 = part.strip().partition(":")
        if not sep or not kid or kid in seen or ":" in kid:
            raise ConfigError("HEVY_KEY_ENCRYPTION_KEYS は 'kid:base64鍵,kid2:base64鍵' の形式で指定してください")
        seen.add(kid)
        keys.append((kid, _b64key(b64)))
    return keys


def load_config(env=None):
    env = os.environ if env is None else env
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
    secure_default = "0" if dev else "1"
    return Config(
        db_path=env.get("DATABASE_PATH", "data/app.db"),
        enc_keys=parse_enc_keys(env),
        public_base_url=base_url,
        cookie_secure=env.get("COOKIE_SECURE", secure_default) not in ("0", "false", "False"),
        session_ttl_days=int(env.get("SESSION_TTL_DAYS", "14")),
        hevy_base=hevy_base,
        app_env=app_env,
        page_delay=float(env.get("HEVY_PAGE_DELAY", "0.25")),
        min_sync_interval=int(env.get("MIN_SYNC_INTERVAL_SECONDS", "10")),
        static_root=env.get("STATIC_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    )
