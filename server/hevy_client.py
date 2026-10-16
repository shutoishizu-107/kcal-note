"""Hevy公式API（読み取りのGETのみ）へのクライアント。接続先は設定値のHevyのベースURL固定で、任意URLへは送らない。"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

ALLOWED_PATHS = ("/v1/user/info", "/v1/exercise_templates", "/v1/workouts", "/v1/workouts/events")


class HevyError(Exception):
    """kind: auth / rate / server / network / bad"""

    def __init__(self, kind, message, retry_after=None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


class HevyClient:
    def __init__(self, base, api_key, sleep=time.sleep, timeout=30, max_attempts=4, max_wait=20):
        self.base, self._key = base, api_key
        self.sleep, self.timeout, self.max_attempts, self.max_wait = sleep, timeout, max_attempts, max_wait

    def __repr__(self):  # キーがログ・例外表示に出ないように
        return "<HevyClient>"

    def get(self, path, params=None):
        if path not in ALLOWED_PATHS:
            raise HevyError("bad", "許可されていないパスです")
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        last = None
        for attempt in range(1, self.max_attempts + 1):
            req = urllib.request.Request(url, method="GET", headers={
                "api-key": self._key, "Accept": "application/json", "User-Agent": "kcal-note-hevy-sync/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as res:
                    return json.loads(res.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                code = e.code
                ra = e.headers.get("Retry-After") if e.headers else None
                e.close()
                if code in (401, 403):
                    raise HevyError("auth", "Hevyが認証を拒否しました。APIキーが無効か、Hevy Proの契約が有効でない可能性があります。")
                if code == 400:
                    raise HevyError("bad", "Hevyがリクエストを受け付けませんでした (400)")
                if code == 429 or 500 <= code < 600:
                    wait = int(ra) if ra and ra.strip().isdigit() else min(2 ** attempt, self.max_wait)
                    wait = min(wait, self.max_wait)
                    last = HevyError("rate" if code == 429 else "server", f"Hevyが混み合っています (HTTP {code})。時間をおいて再試行してください。", wait)
                    if attempt < self.max_attempts:
                        self.sleep(wait)
                        continue
                    raise last
                raise HevyError("server", f"Hevyが想定外のエラーを返しました (HTTP {code})")
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise HevyError("bad", "Hevyの応答を解釈できませんでした")
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                last = HevyError("network", "Hevyに接続できませんでした。通信状況を確認してください。")
                if attempt < self.max_attempts:
                    self.sleep(min(2 ** attempt, self.max_wait))
                    continue
                raise last
        raise last or HevyError("network", "Hevyに接続できませんでした。")
