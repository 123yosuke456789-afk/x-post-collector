#!/usr/bin/env python3
"""status.py — 直近の取得状況を人間向けに表示する

使い方:
    python status.py        (Windows 含むすべての OS)
    bash status.sh          (macOS / Linux / Git Bash 用ラッパー)
"""
import json
from datetime import date
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"


def fmt_money(usd: float) -> str:
    return f"${usd:.2f} (約{int(usd * 150):,}円)"


def main() -> None:
    if not DATA.exists():
        print("まだ実行履歴がありません（data/ が存在しない）")
        return

    cache_path = DATA / "users_cache.json"
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        print(f"📋 ユーザーキャッシュ: {len(cache.get('users', {}))} 件")
    else:
        print("📋 ユーザーキャッシュ: まだありません")

    since_path = DATA / "since_ids.json"
    if since_path.exists():
        since = json.loads(since_path.read_text(encoding="utf-8"))
        print(f"📍 since_id: {len(since)} バッチ分を記録済み")
    else:
        print("📍 since_id: まだありません（次回が初回扱い）")

    today_dir = DATA / date.today().strftime("%Y-%m-%d")
    if not today_dir.exists():
        print(f"\n📭 本日（{date.today()}）の実行はまだありません")
    else:
        runs = sorted(p for p in today_dir.iterdir() if p.is_dir() and p.name.startswith("run_"))
        print(f"\n📅 本日（{date.today()}）の実行: {len(runs)} 回")
        for run in runs[-5:]:
            manifest_path = run / "manifest.json"
            if not manifest_path.exists():
                print(f"   {run.name}: マニフェストなし")
                continue
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
            cost = m.get("estimated_cost", {})
            status_icon = "✅" if m.get("failed_batches", 0) == 0 else "⚠️ "
            print(
                f"   {status_icon} {run.name}: "
                f"{m.get('total_tweets', 0)} 件取得 / "
                f"失敗 {m.get('failed_batches', 0)} バッチ / "
                f"課金 {fmt_money(cost.get('total_usd', 0))} / "
                f"{m.get('elapsed_seconds', 0)}秒"
            )

    print("\n📊 過去3日の最新実行:")
    date_dirs = sorted(
        [p for p in DATA.iterdir() if p.is_dir() and p.name.count("-") == 2],
        reverse=True,
    )
    for d in date_dirs[:3]:
        runs = sorted(p for p in d.iterdir() if p.is_dir() and p.name.startswith("run_"))
        if not runs:
            continue
        latest = runs[-1]
        manifest_path = latest / "manifest.json"
        if not manifest_path.exists():
            continue
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
        cost = m.get("estimated_cost", {})
        print(
            f"   {d.name}: 最新ラン {latest.name} → "
            f"{m.get('total_tweets', 0)} 件 / "
            f"課金 {fmt_money(cost.get('total_usd', 0))}"
        )


if __name__ == "__main__":
    main()
