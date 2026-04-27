"""
X Post Collector
================
指定アカウント群の最新ポストを X API v2 で定期取得し、
日付・実行単位別の JSON ファイルに保存する。

【使い方】
  python collector.py                   # 通常実行
  python collector.py --dry-run         # API を呼ばずに動作確認
  python collector.py --accounts config/accounts.json  # アカウントファイル指定

【設定】
  config/accounts.json  監視対象アカウント（username）リスト
  .env                  X_BEARER_TOKEN を記載

【保存先】
  data/YYYY-MM-DD/run_HH-MM-SS/batch_001.json
  data/YYYY-MM-DD/run_HH-MM-SS/summary.json

【cron（launchd）設定例】
  1日2回（8:00 と 20:00）実行する場合:
  -> plist で ProgramArguments に python collector.py を指定
  -> StartCalendarInterval で Hour: 8 と Hour: 20 を設定
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import tweepy
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

SCRIPT_DIR    = Path(__file__).parent
CONFIG_PATH   = SCRIPT_DIR / "config" / "accounts.json"
DATA_DIR      = SCRIPT_DIR / "data"
LOG_DIR       = SCRIPT_DIR / "logs"
SINCE_ID_PATH = SCRIPT_DIR / "data" / "since_ids.json"

# X Search API の 1 クエリあたりの文字数上限（余裕を持って設定）
MAX_QUERY_LENGTH = 480

# レートリミット超過時の待機秒数
RATE_LIMIT_WAIT_SECONDS = 60

# 1 バッチあたりの最大取得件数（API 上限は 100）
MAX_RESULTS_PER_BATCH = 100

# ---------------------------------------------------------------------------
# ログ設定
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# バッチ分割
# ---------------------------------------------------------------------------

def split_into_batches(accounts: list[str], max_query_length: int = MAX_QUERY_LENGTH) -> list[list[str]]:
    """
    アカウントリストを X Search API のクエリ長上限に収まるバッチに分割する。

    X の Search API は "from:a OR from:b OR from:c ..." の形式でまとめてクエリできる。
    クエリ文字数の上限を超えないよう、アカウントを複数バッチに分ける。

    例: 1000 アカウント → 約 17 バッチ（1 バッチ = 約 58〜60 アカウント）
    """
    batches: list[list[str]] = []
    current_batch: list[str] = []
    current_length = 0

    for account in accounts:
        fragment = f"from:{account}"
        # OR 区切りを考慮した追加長（最初の要素は " OR " が不要）
        added_length = len(fragment) if not current_batch else len(" OR ") + len(fragment)

        if current_batch and current_length + added_length > max_query_length:
            batches.append(current_batch)
            current_batch = [account]
            current_length = len(fragment)
        else:
            current_batch.append(account)
            current_length += added_length

    if current_batch:
        batches.append(current_batch)

    return batches


def build_query(batch: list[str]) -> str:
    """バッチからX Search APIクエリ文字列を生成する。リプライ・リポストを除外。"""
    from_clause = " OR ".join(f"from:{a}" for a in batch)
    # -is:retweet: リポスト除外 / -is:reply: リプライ除外
    return f"({from_clause}) -is:retweet -is:reply"


# ---------------------------------------------------------------------------
# since_id の管理（重複取得防止）
# ---------------------------------------------------------------------------

def load_since_ids() -> dict[str, str]:
    """前回実行時の最新ツイートIDを読み込む。"""
    if not SINCE_ID_PATH.exists():
        return {}
    try:
        return json.loads(SINCE_ID_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"since_ids.json の読み込みに失敗しました: {e}")
        return {}


def save_since_ids(since_ids: dict[str, str]) -> None:
    """最新ツイートIDを保存する。"""
    SINCE_ID_PATH.parent.mkdir(parents=True, exist_ok=True)
    SINCE_ID_PATH.write_text(json.dumps(since_ids, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# X API クライアント
# ---------------------------------------------------------------------------

def get_client() -> tweepy.Client:
    """Bearer Token で認証した tweepy.Client を返す。"""
    load_dotenv(SCRIPT_DIR / ".env")
    bearer_token = os.getenv("X_BEARER_TOKEN")
    if not bearer_token or bearer_token.startswith("ここに"):
        raise EnvironmentError(
            ".env に X_BEARER_TOKEN が設定されていません。\n"
            ".env.example をコピーして .env を作成し、Bearer Token を設定してください。"
        )
    return tweepy.Client(bearer_token=bearer_token, wait_on_rate_limit=False)


# ---------------------------------------------------------------------------
# 1 バッチ分の取得処理
# ---------------------------------------------------------------------------

def fetch_batch(
    client: tweepy.Client,
    batch: list[str],
    batch_index: int,
    since_id: str | None,
    dry_run: bool,
) -> tuple[list[dict], str | None]:
    """
    1 バッチ分のアカウントのポストを取得する。

    Returns:
        (tweets_list, newest_id)
        tweets_list: 取得したツイートデータのリスト
        newest_id:   このバッチで最も新しいツイートの ID（次回の since_id として使う）
    """
    query = build_query(batch)
    log.info(f"  バッチ {batch_index:03d}: {len(batch)} アカウント / クエリ長 {len(query)} 文字")

    if dry_run:
        log.info(f"  [dry-run] クエリ: {query[:120]}{'...' if len(query) > 120 else ''}")
        return [], None

    for attempt in range(3):
        try:
            response = client.search_recent_tweets(
                query=query,
                max_results=MAX_RESULTS_PER_BATCH,
                since_id=since_id,
                tweet_fields=["created_at", "author_id", "text", "attachments"],
                expansions=["attachments.media_keys", "author_id"],
                media_fields=["url", "preview_image_url", "type"],
                user_fields=["username"],
            )
            break
        except tweepy.errors.TooManyRequests:
            wait = RATE_LIMIT_WAIT_SECONDS * (attempt + 1)
            log.warning(f"  レートリミット超過。{wait}秒後にリトライします（{attempt + 1}/3）")
            time.sleep(wait)
        except tweepy.errors.TwitterServerError as e:
            log.warning(f"  X サーバーエラー: {e}。15秒後にリトライします（{attempt + 1}/3）")
            time.sleep(15)
    else:
        log.error(f"  バッチ {batch_index:03d}: 3回リトライしても失敗。このバッチをスキップします。")
        return [], None

    if not response.data:
        log.info(f"  バッチ {batch_index:03d}: 新規ツイートなし")
        return [], None

    # ユーザー情報を辞書化（author_id → username の対応）
    users = {}
    if response.includes and "users" in response.includes:
        for user in response.includes["users"]:
            users[str(user.id)] = user.username

    # メディア情報を辞書化（media_key → URL の対応）
    media_map = {}
    if response.includes and "media" in response.includes:
        for media in response.includes["media"]:
            url = getattr(media, "url", None) or getattr(media, "preview_image_url", None)
            media_map[media.media_key] = {
                "type": media.type,
                "url": url,
            }

    tweets = []
    for tweet in response.data:
        attachments = getattr(tweet, "attachments", None) or {}
        media_keys = attachments.get("media_keys", []) if isinstance(attachments, dict) else []
        media_urls = [media_map[k] for k in media_keys if k in media_map]

        tweets.append({
            "id": str(tweet.id),
            "author_id": str(tweet.author_id),
            "username": users.get(str(tweet.author_id), ""),
            "text": tweet.text,
            "created_at": tweet.created_at.isoformat() if tweet.created_at else None,
            "media": media_urls,
        })

    newest_id = str(response.meta.get("newest_id")) if response.meta else None
    log.info(f"  バッチ {batch_index:03d}: {len(tweets)} 件取得")
    return tweets, newest_id


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------

def run(accounts_path: Path = CONFIG_PATH, dry_run: bool = False) -> None:
    run_start = datetime.now()
    log.info(f"=== X Post Collector 開始 {'[dry-run]' if dry_run else ''} ===")

    # アカウントリスト読み込み
    if not accounts_path.exists():
        log.error(f"アカウント設定ファイルが見つかりません: {accounts_path}")
        sys.exit(1)

    accounts_data = json.loads(accounts_path.read_text(encoding="utf-8"))
    accounts: list[str] = accounts_data.get("accounts", [])
    if not accounts:
        log.error("accounts.json にアカウントが1件もありません。")
        sys.exit(1)

    log.info(f"監視アカウント数: {len(accounts)} 件")

    # バッチ分割
    batches = split_into_batches(accounts)
    log.info(f"バッチ数: {len(batches)} 個（1バッチあたり最大 {max(len(b) for b in batches)} アカウント）")
    log.info(f"1日2回実行時の推定リクエスト数: {len(batches) * 2} 件/日")

    if dry_run:
        log.info("[dry-run] API は呼び出しません。バッチ構造の確認のみ行います。")
        for i, batch in enumerate(batches, 1):
            query = build_query(batch)
            log.info(f"  バッチ {i:03d}: {len(batch)} アカウント / クエリ長 {len(query)} 文字")
        log.info(f"[dry-run] 完了。保存先: data/{run_start.strftime('%Y-%m-%d')}/run_{run_start.strftime('%H-%M-%S')}/")
        return

    # X API クライアント初期化
    client = get_client()

    # since_ids 読み込み（重複取得防止）
    since_ids = load_since_ids()

    # 保存ディレクトリ
    run_dir = DATA_DIR / run_start.strftime("%Y-%m-%d") / f"run_{run_start.strftime('%H-%M-%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # バッチごとに取得・保存
    total_tweets = 0
    failed_batches = 0
    new_since_ids: dict[str, str] = {}

    for i, batch in enumerate(batches, 1):
        batch_key = f"batch_{i:03d}"
        since_id = since_ids.get(batch_key)

        tweets, newest_id = fetch_batch(client, batch, i, since_id, dry_run=False)

        if newest_id:
            new_since_ids[batch_key] = newest_id

        if tweets:
            out_path = run_dir / f"{batch_key}.json"
            out_path.write_text(
                json.dumps({
                    "fetched_at": run_start.isoformat(),
                    "batch_index": i,
                    "accounts": batch,
                    "tweet_count": len(tweets),
                    "tweets": tweets,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            total_tweets += len(tweets)
        elif tweets is None:
            failed_batches += 1

        # レートリミットを超えないよう少し待機
        time.sleep(1)

    # since_ids を更新（取得できたバッチ分だけ上書き）
    since_ids.update(new_since_ids)
    save_since_ids(since_ids)

    # サマリ保存
    summary = {
        "run_at": run_start.isoformat(),
        "account_count": len(accounts),
        "batch_count": len(batches),
        "total_tweets": total_tweets,
        "failed_batches": failed_batches,
        "elapsed_seconds": round((datetime.now() - run_start).total_seconds(), 1),
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log.info(
        f"=== 完了 ==="
        f" 取得 {total_tweets} 件 /"
        f" 失敗バッチ {failed_batches} 個 /"
        f" 経過 {summary['elapsed_seconds']} 秒"
    )


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="X Post Collector")
    parser.add_argument("--dry-run", action="store_true", help="APIを呼ばずに動作確認のみ行う")
    parser.add_argument("--accounts", type=Path, default=CONFIG_PATH, help="アカウントリストのJSONファイルパス")
    args = parser.parse_args()

    run(accounts_path=args.accounts, dry_run=args.dry_run)
