"""パスワードハッシュ、トークン、APIキーの認証付き暗号化（AES-256-GCM）。独自暗号は使わない。"""
import base64
import hashlib
import hmac
import os
import re
import secrets
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
MIN_PASSWORD = 10
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


def hash_password(password):
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32, maxmem=64 * 1024 * 1024)
    return "scrypt${}${}${}${}${}".format(SCRYPT_N, SCRYPT_R, SCRYPT_P, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password, stored):
    try:
        _, n, r, p, salt, dk = stored.split("$")
        calc = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32, maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


DUMMY_HASH = hash_password("dummy-password-for-timing")


def check_password_policy(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        return f"パスワードは{MIN_PASSWORD}文字以上にしてください"
    if len(password) > 200:
        return "パスワードが長すぎます"
    return None


def new_token():
    return secrets.token_urlsafe(32)


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def valid_hevy_key(text):
    """Hevy公式仕様でapi-keyは uuid 形式。形式が違うものは通信前に弾く。"""
    if not isinstance(text, str):
        return None
    t = text.strip()
    try:
        return str(uuid.UUID(t))
    except ValueError:
        return None


class KeyRing:
    """AES-256-GCM。暗号文は 'v1:<kid>:<base64(nonce+ct)>'。AADに用途と利用者IDを入れ、別利用者への流用を防ぐ。"""

    def __init__(self, keys):
        self.keys = dict(keys)
        self.current = keys[0][0]

    @staticmethod
    def _aad(purpose, user_id):
        return f"{purpose}:{user_id}".encode("utf-8")

    def encrypt(self, plaintext, purpose, user_id):
        nonce = os.urandom(12)
        ct = AESGCM(self.keys[self.current]).encrypt(nonce, plaintext.encode("utf-8"), self._aad(purpose, user_id))
        return "v1:{}:{}".format(self.current, base64.urlsafe_b64encode(nonce + ct).decode())

    def decrypt(self, blob, purpose, user_id):
        ver, kid, data = blob.split(":", 2)
        if ver != "v1" or kid not in self.keys:
            raise ValueError("unknown key id")
        raw = base64.urlsafe_b64decode(data)
        return AESGCM(self.keys[kid]).decrypt(raw[:12], raw[12:], self._aad(purpose, user_id)).decode("utf-8")

    def needs_rotation(self, blob):
        return blob.split(":", 2)[1] != self.current


def generate_key():
    return base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
