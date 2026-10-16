#!/usr/bin/env python3
"""Hevy公式APIの種目テンプレート一覧を取得し、hevy-exercises.json に保存する。

- 使うのは GET /v1/exercise_templates のみ（任意で GET /v1/user/info）。書き込み系は呼ばない。
- APIキーは環境変数 HEVY_API_KEY から読む。出力・ログには含めない。
- 失敗時は既存の出力ファイルを変更しない（検証後に一時ファイルから置換）。

使い方:
    py tools/fetch_hevy_exercises.py [--out hevy-exercises.json]

標準ライブラリのみで動作する（Python 3.9+）。
"""
import argparse
import datetime
import hashlib
import json
import os
import random
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

FORMAT = "hevy-exercise-templates"
SCHEMA_VERSION = 1
DEFAULT_BASE = "https://api.hevyapp.com"
PAGE_SIZE = 100  # 公式仕様: pageSize の最大値
MAX_PAGES = 1000
KNOWN_FIELDS = {
    "id", "title", "type", "primary_muscle_group", "secondary_muscle_groups",
    "equipment", "is_custom", "thumbnail_url",
}


class FetchError(Exception):
    pass


def _retry_after(err):
    value = err.headers.get("Retry-After") if err.headers else None
    if value and value.strip().isdigit():
        return min(int(value.strip()), 120)
    return None


def http_get_json(url, api_key, timeout=30, max_attempts=5, sleep=time.sleep, log=print):
    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(
            url,
            headers={"api-key": api_key, "Accept": "application/json",
                     "User-Agent": "kcal-note-hevy-exercise-fetch/1.0"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                body = res.read()
            return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as e:
            code, wait = e.code, _retry_after(e)
            e.close()
            if code in (401, 403):
                raise FetchError(f"認証に失敗しました (HTTP {code})。APIキーとHevy Proの有効状態を確認してください。")
            if code == 400:
                raise FetchError("リクエストが不正です (HTTP 400)。")
            if code == 429 or 500 <= code < 600:
                if attempt == max_attempts:
                    raise FetchError(f"HTTP {code} が続いたため中止しました。時間をおいて再実行してください。")
                if wait is None:
                    wait = min(2 ** attempt, 60)
                log(f"  HTTP {code}: {wait}秒待って再試行します ({attempt}/{max_attempts - 1})")
                sleep(wait)
                continue
            raise FetchError(f"想定外のHTTPエラー (HTTP {code})")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise FetchError("レスポンスをJSONとして解釈できません。")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            if attempt == max_attempts:
                raise FetchError("通信に失敗しました。ネットワーク接続を確認してください。")
            wait = min(2 ** attempt, 60)
            log(f"  通信エラー: {wait}秒待って再試行します ({attempt}/{max_attempts - 1})")
            sleep(wait)
    raise FetchError("通信に失敗しました。")


def _opt_str(value, field, idx):
    if value is None or isinstance(value, str):
        return value
    raise FetchError(f"{idx}件目: {field} の型が想定外です。")


def normalize_template(raw, idx):
    if not isinstance(raw, dict):
        raise FetchError(f"{idx}件目: 種目がオブジェクトではありません。")
    tid, title = raw.get("id"), raw.get("title")
    if not isinstance(tid, str) or not tid.strip():
        raise FetchError(f"{idx}件目: id がありません。")
    if not isinstance(title, str) or not title.strip():
        raise FetchError(f"{idx}件目: title がありません。")
    secondary = raw.get("secondary_muscle_groups")
    if secondary is None:
        secondary = []
    if not isinstance(secondary, list) or not all(isinstance(s, str) for s in secondary):
        raise FetchError(f"{idx}件目: secondary_muscle_groups の型が想定外です。")
    is_custom = raw.get("is_custom")
    if is_custom is not None and not isinstance(is_custom, bool):
        raise FetchError(f"{idx}件目: is_custom の型が想定外です。")
    return {
        "hevyId": tid.strip(),
        "title": title,
        "type": _opt_str(raw.get("type"), "type", idx),
        "primaryMuscleGroup": _opt_str(raw.get("primary_muscle_group"), "primary_muscle_group", idx),
        "secondaryMuscleGroups": secondary,
        "equipment": _opt_str(raw.get("equipment"), "equipment", idx),
        "isCustom": is_custom,
        "thumbnailUrl": _opt_str(raw.get("thumbnail_url"), "thumbnail_url", idx),
        # 公式スキーマで確認できた以外のフィールドは落とさず保持する
        "extra": {k: v for k, v in raw.items() if k not in KNOWN_FIELDS},
    }


def fetch_all(base, api_key, page_size=PAGE_SIZE, delay=0.3, sleep=time.sleep, log=print):
    """全ページを取得し、id で重複排除した種目リストを返す。"""
    by_id, order = {}, []
    duplicates, page, page_count, total_seen = 0, 1, None, 0
    while True:
        query = urllib.parse.urlencode({"page": page, "pageSize": page_size})
        data = http_get_json(f"{base}/v1/exercise_templates?{query}", api_key, sleep=sleep, log=log)
        items = data.get("exercise_templates") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise FetchError("レスポンスに exercise_templates 配列がありません。")
        pc = data.get("page_count")
        if not isinstance(pc, int) or isinstance(pc, bool) or pc < 1:
            raise FetchError("レスポンスの page_count が不正です。")
        if page_count is None:
            page_count = pc
            if page_count > MAX_PAGES:
                raise FetchError("ページ数が異常に多いため中止しました。")
        for raw in items:
            total_seen += 1
            rec = normalize_template(raw, total_seen)
            if rec["hevyId"] in by_id:
                duplicates += 1
                continue
            by_id[rec["hevyId"]] = rec
            order.append(rec["hevyId"])
        log(f"  ページ {page}/{page_count}: {len(items)}件")
        if page >= page_count:
            break
        page += 1
        sleep(delay)
    if not order:
        raise FetchError("種目が0件でした。既存ファイルは変更しません。")
    return [by_id[i] for i in order], {"pagesFetched": page, "duplicatesSkipped": duplicates}


def fetch_account_key(base, api_key, sleep=time.sleep, log=print):
    """GET /v1/user/info のユーザーIDをハッシュ化して返す。カスタム種目IDの名前空間用。失敗時は None。"""
    try:
        data = http_get_json(f"{base}/v1/user/info", api_key, max_attempts=2, sleep=sleep, log=log)
        uid = data.get("data", {}).get("id") if isinstance(data, dict) else None
        if isinstance(uid, str) and uid:
            return hashlib.sha256(uid.encode("utf-8")).hexdigest()[:16]
    except FetchError as e:
        log(f"  警告: ユーザー情報を取得できませんでした（{e}）。カスタム種目の名前空間は 'unknown' になります。")
    return None


def build_output(exercises, meta, account_key, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return {
        "format": FORMAT,
        "schemaVersion": SCHEMA_VERSION,
        "source": "hevy-api",
        "endpoint": "GET /v1/exercise_templates",
        "fetchedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "accountKey": account_key,
        "count": len(exercises),
        "pagesFetched": meta["pagesFetched"],
        "duplicatesSkipped": meta["duplicatesSkipped"],
        "exercises": exercises,
    }


def write_atomic(path, obj):
    """検証済みの内容を一時ファイルへ書き、成功したときだけ置換する。"""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".hevy-exercises-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.write("\n")
        with open(tmp, encoding="utf-8") as f:
            check = json.load(f)
        if check.get("count") != len(check.get("exercises", [])):
            raise FetchError("出力の整合性チェックに失敗しました。")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _check_base(base):
    p = urllib.parse.urlparse(base)
    local = p.hostname in ("localhost", "127.0.0.1")
    if p.scheme != "https" and not (p.scheme == "http" and local):
        raise FetchError("APIキーの漏えい防止のため、https 以外の接続先は使えません（localhost のテスト用を除く）。")
    return base.rstrip("/")


def main(argv=None, env=None, sleep=time.sleep, out=sys.stdout, err=sys.stderr):
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(description="Hevy種目テンプレートを取得して JSON に保存します（GETのみ）。")
    ap.add_argument("--out", default="hevy-exercises.json")
    ap.add_argument("--base-url", default=env.get("HEVY_API_BASE", DEFAULT_BASE), help="テスト用。通常は指定不要")
    ap.add_argument("--page-size", type=int, default=PAGE_SIZE)
    ap.add_argument("--no-account", action="store_true", help="GET /v1/user/info を呼ばない")
    ap.add_argument("--no-jitter", action="store_true", help="毎時0分の待機を行わない")
    args = ap.parse_args(argv)

    def log(msg):
        print(msg, file=out)

    key = env.get("HEVY_API_KEY", "").strip()
    if not key:
        print("エラー: 環境変数 HEVY_API_KEY が設定されていません。", file=err)
        return 2
    if not 1 <= args.page_size <= PAGE_SIZE:
        print(f"エラー: --page-size は 1〜{PAGE_SIZE} です。", file=err)
        return 2

    def redact(msg):
        return str(msg).replace(key, "***")

    try:
        base = _check_base(args.base_url)
        now = datetime.datetime.now()
        if now.minute == 0 and not args.no_jitter:
            wait = random.randint(60, 240)  # 公式の注意: 毎時0分ちょうどのアクセスを避ける
            log(f"毎時0分のため {wait}秒待機します。")
            sleep(wait)
        log("種目テンプレートを取得中...")
        exercises, meta = fetch_all(base, key, page_size=args.page_size, sleep=sleep, log=log)
        account_key = None if args.no_account else fetch_account_key(base, key, sleep=sleep, log=log)
        result = build_output(exercises, meta, account_key)
        write_atomic(args.out, result)
    except FetchError as e:
        print(f"エラー: {redact(e)}", file=err)
        return 1
    except Exception as e:  # 想定外でも既存ファイルとキーを守る
        print(f"想定外のエラー: {type(e).__name__}", file=err)
        return 1
    log(f"完了: {result['count']}件を {args.out} に保存しました（重複スキップ {meta['duplicatesSkipped']}件）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
