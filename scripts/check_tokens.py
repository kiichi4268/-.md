#!/usr/bin/env python3
"""Instagram（Meta）と Threads のアクセストークンの有効性・有効期限を確認する。

- Instagram: Graph API の debug_token で有効期限（expires_at）とデータアクセス期限（data_access_expires_at）を確認
- Threads:   API を1回呼んで有効性を確認。期限は Repository variable `THREADS_TOKEN_EXPIRES_ON`
             （YYYY-MM-DD。トークンを発行・更新した日の60日後）を設定すると判定に使う

残り WARN_DAYS 日を切ったら・無効なら終了コード 1（GitHub Actions のジョブが失敗し、通知メールが届く）。

環境変数:
  META_ACCESS_TOKEN, THREADS_ACCESS_TOKEN   確認するトークン（未設定のものはスキップ）
  META_APP_ID, META_APP_SECRET               任意。設定するとアプリトークンで debug_token を呼ぶ
  THREADS_TOKEN_EXPIRES_ON                   任意。Threads トークンの期限（YYYY-MM-DD）
  GRAPH_API_VERSION / THREADS_API_VERSION    既定 v23.0 / v1.0
  WARN_DAYS                                  既定 14
"""

import datetime as dt
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from post_to_sns import GRAPH_BASE, THREADS_BASE, http_request, write_summary  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))


def days_left(ts: dt.datetime) -> int:
    return math.ceil((ts - dt.datetime.now(JST)).total_seconds() / 86400)


def check_instagram(token: str, warn_days: int) -> tuple:
    version = os.environ.get("GRAPH_API_VERSION", "v23.0")
    app_id, app_secret = os.environ.get("META_APP_ID"), os.environ.get("META_APP_SECRET")
    inspector = f"{app_id}|{app_secret}" if app_id and app_secret else token
    data = http_request("GET", f"{GRAPH_BASE}/{version}/debug_token",
                        {"input_token": token, "access_token": inspector})["data"]
    if not data.get("is_valid"):
        msg = (data.get("error") or {}).get("message", "トークンが無効です")
        return False, f"無効: {msg}（トークンを再発行して Secrets の META_ACCESS_TOKEN を更新してください）"

    notes, ok = [], True
    for key, label in (("expires_at", "有効期限"), ("data_access_expires_at", "データアクセス期限")):
        ts = data.get(key) or 0
        if not ts:
            notes.append(f"{label}: 無期限")
            continue
        when = dt.datetime.fromtimestamp(ts, JST)
        left = days_left(when)
        notes.append(f"{label}: {when:%Y-%m-%d}（残り{left}日）")
        if left < warn_days:
            ok = False
    if not ok:
        notes.append("→ 期限が近いため、トークンを再発行（長期トークンに交換）して Secrets を更新してください")
    return ok, " / ".join(notes)


def check_threads(token: str, warn_days: int) -> tuple:
    version = os.environ.get("THREADS_API_VERSION", "v1.0")
    me = http_request("GET", f"{THREADS_BASE}/{version}/me", {"fields": "id,username", "access_token": token})
    note = f"有効（@{me.get('username', me.get('id'))}）"
    expires_on = os.environ.get("THREADS_TOKEN_EXPIRES_ON", "").strip()
    if not expires_on:
        return True, note + " / 期限: 不明（Repository variable THREADS_TOKEN_EXPIRES_ON を設定すると監視できます）"
    when = dt.datetime.fromisoformat(expires_on).replace(tzinfo=JST)
    left = days_left(when)
    note += f" / 期限: {expires_on}（残り{left}日）"
    if left < warn_days:
        return False, note + " → 期限が近いため、長期トークンを更新して Secrets と THREADS_TOKEN_EXPIRES_ON を更新してください"
    return True, note


def main() -> int:
    warn_days = int(os.environ.get("WARN_DAYS", "14"))
    rows, failed = [], False
    for name, env, checker in (("Instagram", "META_ACCESS_TOKEN", check_instagram),
                               ("Threads", "THREADS_ACCESS_TOKEN", check_threads)):
        token = os.environ.get(env)
        if not token:
            rows.append((name, "⏭ スキップ", f"{env} が未設定"))
            continue
        try:
            ok, note = checker(token, warn_days)
        except Exception as e:  # noqa: BLE001
            ok, note = False, str(e)
        rows.append((name, "✅ OK" if ok else "❌ 要対応", note))
        failed |= not ok

    for name, status, note in rows:
        print(f"{status} {name}: {note}")
        if status.startswith("❌"):
            print(f"::error::{name} のトークン: {note}")
    write_summary(f"## 🔑 アクセストークンの確認（残り{warn_days}日未満で警告）", ["サービス", "結果", "詳細"], rows)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
