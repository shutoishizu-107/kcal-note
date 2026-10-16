"""Hevy同期。ネットワーク取得を先に全部行い、成功後に1トランザクションで反映する（途中失敗で既存データ・同期位置を壊さない）。

方針（公式仕様 https://api.hevyapp.com/docs/ で確認した範囲）:
- GET /v1/workouts は pageSize 最大10、並び順は仕様に記載がないため全ページ取得してから開始日で絞る。
- GET /v1/workouts/events は `since` 以降の updated / deleted を新しい順で返す。古い順に並べ直して適用する。
- 削除は events の deleted だけを根拠にする（一覧に無いだけでは削除しない）。
- 同期位置(cursor)は「取得開始時刻」。取得中に変更があっても次回の events で拾える。
"""
import datetime
import json
import secrets
import time

from .hevy_client import HevyClient, HevyError

UTC = datetime.timezone.utc
WORKOUT_PAGE = 10
TEMPLATE_PAGE = 100
MAX_PAGES = 5000
TEMPLATE_TTL = datetime.timedelta(hours=12)
EVENTS_OVERLAP = datetime.timedelta(seconds=60)
STALE_LOCK = datetime.timedelta(minutes=5)


class SyncError(Exception):
    def __init__(self, code, message, retry_after=None):
        super().__init__(message)
        self.code, self.message, self.retry_after = code, message, retry_after


def utcnow():
    return datetime.datetime.now(UTC)


def iso(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def fetch_pages(client, path, key, params, page_size, delay, sleep):
    page, page_count, out = 1, None, []
    while True:
        data = client.get(path, {**params, "page": page, "pageSize": page_size})
        items = data.get(key) if isinstance(data, dict) else None
        # /workouts/events は公式仕様では events だが、提供側の移行中の
        # workout_events / workoutEvents と data.events も読み取り専用で受容する。
        # 別の配列を推測して取り込むことはせず、記録の消失を防ぐ。
        if key == "events" and isinstance(data, dict) and not isinstance(items, list):
            for alias in ("workout_events", "workoutEvents"):
                if isinstance(data.get(alias), list):
                    items = data[alias]
                    break
            else:
                nested = data.get("data")
                if isinstance(nested, dict) and isinstance(nested.get("events"), list):
                    items = nested["events"]
                # Hevy が events エンドポイントから workouts を返すケース。
                # 削除の推測はせず、各レコードを更新イベントとしてのみ反映する。
                elif isinstance(data.get("workouts"), list):
                    items = [{"type": "updated", "workout": w} for w in data["workouts"]]
        # Hevy の公式仕様は page_count だが、API の段階的な変更・空結果で
        # ページ情報が省略される応答にも対応する。空でないページのページ数が
        # 不明な場合は続きの取りこぼしを避けるため、従来どおり失敗にする。
        pc = data.get("page_count", data.get("pageCount")) if isinstance(data, dict) else None
        if isinstance(pc, str) and pc.isdecimal():
            pc = int(pc)
        if not isinstance(items, list):
            fields = ", ".join(sorted(str(k) for k in data)[:8]) if isinstance(data, dict) else type(data).__name__
            raise HevyError("bad", f"Hevyの応答形式が想定と異なります ({path}: {key} が配列ではありません。受信項目: {fields or 'なし'})")
        if pc is None and not items:
            return out
        if not isinstance(pc, int) or isinstance(pc, bool) or pc < 0:
            raise HevyError("bad", f"Hevyの応答形式が想定と異なります ({path}: page_count が不正です)")
        if page_count is None:
            page_count = pc
            if page_count > MAX_PAGES:
                raise HevyError("bad", "ページ数が異常に多いため中止しました")
        elif pc != page_count:
            raise HevyError("bad", f"Hevyの応答形式が想定と異なります ({path}: page_count が途中で変わりました)")
        out.extend(items)
        if page >= page_count:
            return out
        page += 1
        if delay:
            sleep(delay)


def normalize_user(data):
    d = data.get("data") if isinstance(data, dict) else None
    if not isinstance(d, dict) or not isinstance(d.get("id"), str) or not d["id"]:
        raise HevyError("bad", "Hevyのアカウント情報を取得できませんでした")
    return {"id": d["id"], "username": d.get("username") if isinstance(d.get("username"), str) else None,
            "name": d.get("name") if isinstance(d.get("name"), str) else None,
            "isPro": d.get("is_pro") if isinstance(d.get("is_pro"), bool) else None}


def valid_workout(w):
    return isinstance(w, dict) and isinstance(w.get("id"), str) and w["id"] and parse_iso(w.get("start_time")) is not None


def get_state(conn, user_id, account_id, create=True):
    row = conn.execute("SELECT * FROM hevy_state WHERE user_id=? AND account_id=?", (user_id, account_id)).fetchone()
    if row or not create:
        return row
    conn.execute("INSERT INTO hevy_state(user_id, account_id, generation) VALUES(?,?,?)", (user_id, account_id, secrets.randbits(30) + 1))
    return conn.execute("SELECT * FROM hevy_state WHERE user_id=? AND account_id=?", (user_id, account_id)).fetchone()


def _upsert(conn, user_id, account, w):
    row = conn.execute("SELECT payload, hevy_updated_at, deleted FROM hevy_workouts WHERE user_id=? AND account_id=? AND workout_id=?",
                       (user_id, account, w["id"])).fetchone()
    if row and not row["deleted"]:
        old, new = parse_iso(row["hevy_updated_at"]), parse_iso(w.get("updated_at"))
        if old and new and new <= old:
            return "unchanged"
    conn.execute("INSERT INTO hevy_workouts(user_id, account_id, workout_id, payload, hevy_updated_at, start_time, deleted) VALUES(?,?,?,?,?,?,0) "
                 "ON CONFLICT(user_id, account_id, workout_id) DO UPDATE SET payload=excluded.payload, hevy_updated_at=excluded.hevy_updated_at, start_time=excluded.start_time, deleted=0",
                 (user_id, account, w["id"], json.dumps(w, ensure_ascii=False), w.get("updated_at"), w.get("start_time")))
    conn.execute("INSERT INTO hevy_changes(user_id, account_id, workout_id, kind) VALUES(?,?,?, 'upsert')", (user_id, account, w["id"]))
    return "updated" if row else "added"


def _mark_deleted(conn, user_id, account, wid):
    row = conn.execute("SELECT deleted FROM hevy_workouts WHERE user_id=? AND account_id=? AND workout_id=?", (user_id, account, wid)).fetchone()
    if not row or row["deleted"]:
        return "ignored"
    conn.execute("UPDATE hevy_workouts SET deleted=1, payload=NULL WHERE user_id=? AND account_id=? AND workout_id=?", (user_id, account, wid))
    conn.execute("INSERT INTO hevy_changes(user_id, account_id, workout_id, kind) VALUES(?,?,?, 'delete')", (user_id, account, wid))
    return "deleted"


def _fetch_templates(client, delay, sleep):
    items = fetch_pages(client, "/v1/exercise_templates", "exercise_templates", {}, TEMPLATE_PAGE, delay, sleep)
    out = {}
    for t in items:
        if isinstance(t, dict) and isinstance(t.get("id"), str) and t["id"] and isinstance(t.get("title"), str):
            out[t["id"]] = t
    return list(out.values())


def run_sync(conn, cfg, ring, user_id, mode="incremental", days=None, now=None, sleep=time.sleep, force_templates=False):
    now = now or utcnow()
    c = conn.execute("SELECT * FROM hevy_connections WHERE user_id=?", (user_id,)).fetchone()
    if not c:
        raise SyncError("not_connected", "Hevy連携が設定されていません")
    account = c["hevy_account_id"]
    st = get_state(conn, user_id, account)
    last = parse_iso(st["last_attempt_at"])
    if last and (now - last).total_seconds() < cfg.min_sync_interval:
        raise SyncError("too_soon", "直前に同期しました。少し待ってから再実行してください。", retry_after=cfg.min_sync_interval)
    cur = conn.execute("UPDATE hevy_state SET syncing_since=?, last_attempt_at=? WHERE user_id=? AND account_id=? AND (syncing_since IS NULL OR syncing_since<?)",
                       (iso(now), iso(now), user_id, account, iso(now - STALE_LOCK)))
    if cur.rowcount == 0:
        raise SyncError("busy", "同期中です。完了までお待ちください。")
    try:
        summary = _do_sync(conn, cfg, ring, c, st, user_id, account, mode, days, now, sleep, force_templates)
        conn.execute("UPDATE hevy_state SET syncing_since=NULL, last_success_at=?, last_error=NULL WHERE user_id=? AND account_id=?",
                     (iso(utcnow()), user_id, account))
        return summary
    except HevyError as e:
        msg = {"auth": "認証エラー: ", "rate": "レート制限: ", "server": "Hevy側エラー: ", "network": "通信エラー: ", "bad": "応答エラー: "}.get(e.kind, "") + str(e)
        conn.execute("UPDATE hevy_state SET syncing_since=NULL, last_error=? WHERE user_id=? AND account_id=?", (msg[:300], user_id, account))
        raise SyncError("hevy_" + e.kind, str(e), retry_after=e.retry_after)
    except Exception:
        conn.execute("UPDATE hevy_state SET syncing_since=NULL, last_error=? WHERE user_id=? AND account_id=?", ("内部エラーが発生しました", user_id, account))
        raise


def _do_sync(conn, cfg, ring, c, st, user_id, account, mode, days, now, sleep, force_templates):
    try:
        api_key = ring.decrypt(c["key_enc"], "hevy-api-key", user_id)
    except Exception:
        raise HevyError("auth", "保存済みのAPIキーを復号できません。APIキーを登録し直してください。")
    client = HevyClient(cfg.hevy_base, api_key, sleep=sleep)
    started = now
    new_from = parse_iso(st["initial_from"])
    new_cursor, workouts, events = st["cursor_since"], None, None
    if not st["initial_done"]:
        new_from = now - datetime.timedelta(days=int(c["initial_days"]))
        workouts = [w for w in fetch_pages(client, "/v1/workouts", "workouts", {}, WORKOUT_PAGE, cfg.page_delay, sleep) if valid_workout(w) and parse_iso(w["start_time"]) >= new_from]
        new_cursor = iso(started)
    elif mode == "backfill":
        d = max(1, min(int(days or 365), 3650))
        target = now - datetime.timedelta(days=d)
        if new_from is None or target < new_from:
            workouts = [w for w in fetch_pages(client, "/v1/workouts", "workouts", {}, WORKOUT_PAGE, cfg.page_delay, sleep) if valid_workout(w) and parse_iso(w["start_time"]) >= target]
            new_from = target
    else:
        since = (parse_iso(st["cursor_since"]) or started) - EVENTS_OVERLAP
        raw = fetch_pages(client, "/v1/workouts/events", "events", {"since": iso(since)}, WORKOUT_PAGE, cfg.page_delay, sleep)
        events = list(reversed(raw))  # 新しい順 → 古い順
        new_cursor = iso(started)
    templates = None
    tat = parse_iso(st["templates_at"])
    if force_templates or tat is None or now - tat > TEMPLATE_TTL or not st["initial_done"] or mode == "backfill":
        templates = _fetch_templates(client, cfg.page_delay, sleep)

    counts = {"added": 0, "updated": 0, "unchanged": 0, "deleted": 0, "ignored": 0, "outOfRange": 0}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for w in workouts or []:
            counts[_upsert(conn, user_id, account, w)] += 1
        init_from = parse_iso(st["initial_from"]) or new_from
        for ev in events or []:
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == "updated" and valid_workout(ev.get("workout")):
                w = ev["workout"]
                known = conn.execute("SELECT 1 FROM hevy_workouts WHERE user_id=? AND account_id=? AND workout_id=?", (user_id, account, w["id"])).fetchone()
                if not known and init_from and parse_iso(w["start_time"]) < init_from:
                    counts["outOfRange"] += 1  # 取り込み対象期間より前の更新は対象外
                    continue
                counts[_upsert(conn, user_id, account, w)] += 1
            elif ev.get("type") == "deleted" and isinstance(ev.get("id"), str) and ev["id"]:
                counts[_mark_deleted(conn, user_id, account, ev["id"])] += 1
        if templates is not None:
            conn.execute("DELETE FROM hevy_templates WHERE user_id=? AND account_id=?", (user_id, account))
            conn.executemany("INSERT INTO hevy_templates(user_id, account_id, template_id, payload) VALUES(?,?,?,?)",
                             [(user_id, account, t["id"], json.dumps(t, ensure_ascii=False)) for t in templates])
        conn.execute("UPDATE hevy_state SET initial_done=1, initial_from=?, cursor_since=?, templates_at=COALESCE(?, templates_at) WHERE user_id=? AND account_id=?",
                     (iso(new_from) if new_from else None, new_cursor, iso(started) if templates is not None else None, user_id, account))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"mode": "initial" if not st["initial_done"] else mode, "rangeFrom": iso(new_from) if new_from else None, "templates": len(templates) if templates is not None else None, **counts}
