"""
X Post Collector
================

X (旧Twitter) の指定アカウント群の最新ポストを定期取得・保存するツール。

【依頼仕様への対応】
- 1000アカウントを OR クエリでバッチ化し、1日のリクエスト数を最小化
- raw API レスポンスを raw/ 配下にそのまま保存（後段の再処理用）
- 加工版を tweets/ 配下に保存（すぐに使える形式）
- アカウント別ビューを by_account/ 配下に生成（アカウント単位で追える）
- 1実行ごとに manifest.json を生成（後段システムが内容を素早く把握できる）
- 設定ファイル（config/accounts.json）でアカウント追加・停止が可能
- 優先度（high/normal/low）でフィルタ実行が可能
- アトミック書き込み（temp + rename）で途中失敗時のデータ破損を防止
- レートリミット時の待機・リトライ、一部バッチ失敗でも全体は完走
"""

import json
import logging
import os
import sys
import time
from datetime import datetime
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
SINCE_ID_PATH = DATA_DIR / "since_ids.json"

MAX_QUERY_LENGTH      = 480   # X Search API のクエリ文字数上限（余裕を見た値）
RATE_LIMIT_WAIT_SECONDS = 60
MAX_RESULTS_PER_BATCH = 100
VALID_PRIORITIES = {"high", "normal", "low"}

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


# ===========================================================================
# 純粋関数（副作用なし・テスト容易）
# ===========================================================================

def normalize_accounts(raw_accounts: list) -> list[dict]:
    """
    accounts.json の中身を正規形 [{"username": ..., "priority": ...}, ...] に変換する。

    旧形式（"username" 文字列）も新形式（{"username":..., "priority":...}）も受け入れる。
    無効な priority 指定は "normal" にフォールバックする。
    """
    normalized = []
    for item in raw_accounts:
        if isinstance(item, str):
            normalized.append({"username": item, "priority": "normal"})
        elif isinstance(item, dict):
            username = item.get("username")
            if not username:
                continue
            priority = item.get("priority", "normal")
            if priority not in VALID_PRIORITIES:
                priority = "normal"
            normalized.append({"username": username, "priority": priority})
    return normalized


def filter_by_priority(accounts: list[dict], priority_filter: str | None) -> list[dict]:
    """優先度でフィルタする。None の場合は全件返す。"""
    if priority_filter is None:
        return accounts
    return [a for a in accounts if a["priority"] == priority_filter]


def split_into_batches(usernames: list[str], max_query_length: int = MAX_QUERY_LENGTH) -> list[list[str]]:
    """
    ユーザー名リストを X Search API のクエリ長制限内のバッチに分割する。
    "from:a OR from:b OR ..." のクエリ長が max_query_length を超えないように分ける。
    """
    batches: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for name in usernames:
        fragment = f"from:{name}"
        added = len(fragment) if not current else len(" OR ") + len(fragment)
        if current and current_len + added > max_query_length:
            batches.append(current)
            current = [name]
            current_len = len(fragment)
        else:
            current.append(name)
            current_len += added
    if current:
        batches.append(current)
    return batches


def build_query(batch: list[str]) -> str:
    """バッチから Search API クエリを生成する。リプライ・リポストを除外。"""
    from_clause = " OR ".join(f"from:{a}" for a in batch)
    return f"({from_clause}) -is:retweet -is:reply"


def atomic_write_text(path: Path, content: str) -> None:
    """temp ファイルに書いてから rename で原子的に置き換える。
    POSIX rename はアトミックなので、書き込み中にクラッシュしても本体は壊れない。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, data) -> None:
    """JSON データをアトミックに書き出す。"""
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# tweepy レスポンスのシリアライズ
# ---------------------------------------------------------------------------

def serialize_response(response) -> dict:
    """
    tweepy.Response を raw JSON 相当の dict に変換する。
    各 tweepy オブジェクトは .data 属性に元のAPIレスポンスのdictを保持しているので、
    それを取り出して raw に近い形を再構成する。
    """
    return {
        "data":     [t.data for t in (response.data or [])],
        "includes": {
            k: [item.data for item in v]
            for k, v in (response.includes or {}).items()
        },
        "meta":     response.meta or {},
        "errors":   [e if isinstance(e, dict) else {"detail": str(e)}
                     for e in (response.errors or [])],
    }


def parse_tweets(response) -> list[dict]:
    """response から、後段で扱いやすい形に整形した tweet リストを返す。"""
    if not response.data:
        return []

    users: dict[str, str] = {}
    if response.includes and "users" in response.includes:
        for user in response.includes["users"]:
            users[str(user.id)] = user.username

    media_map: dict[str, dict] = {}
    if response.includes and "media" in response.includes:
        for media in response.includes["media"]:
            url = getattr(media, "url", None) or getattr(media, "preview_image_url", None)
            media_map[media.media_key] = {"type": media.type, "url": url}

    tweets = []
    for tweet in response.data:
        attachments = getattr(tweet, "attachments", None) or {}
        media_keys = attachments.get("media_keys", []) if isinstance(attachments, dict) else []
        media_urls = [media_map[k] for k in media_keys if k in media_map]
        tweets.append({
            "id":         str(tweet.id),
            "author_id":  str(tweet.author_id),
            "username":   users.get(str(tweet.author_id), ""),
            "text":       tweet.text,
            "created_at": tweet.created_at.isoformat() if tweet.created_at else None,
            "media":      media_urls,
        })
    return tweets


# ===========================================================================
# 副作用あり（ファイル・API）
# ===========================================================================

def load_since_ids() -> dict[str, str]:
    if not SINCE_ID_PATH.exists():
        return {}
    try:
        return json.loads(SINCE_ID_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"since_ids.json の読み込みに失敗しました: {e}")
        return {}


def save_since_ids(since_ids: dict[str, str]) -> None:
    atomic_write_json(SINCE_ID_PATH, since_ids)


def get_client() -> tweepy.Client:
    """Bearer Token で認証した tweepy.Client を返す。"""
    load_dotenv(SCRIPT_DIR / ".env")
    bearer = os.getenv("X_BEARER_TOKEN")
    if not bearer or bearer.startswith("ここに"):
        raise EnvironmentError(
            ".env に X_BEARER_TOKEN が設定されていません。\n"
            ".env.example をコピーして .env を作成し、Bearer Token を設定してください。"
        )
    return tweepy.Client(bearer_token=bearer, wait_on_rate_limit=False)


def fetch_batch(client, batch: list[str], batch_index: int, since_id: str | None):
    """
    1バッチ分のポストを取得する。
    Returns: (response or None, error_message or None)
    """
    query = build_query(batch)
    log.info(f"  バッチ {batch_index:03d}: {len(batch)} アカウント / クエリ長 {len(query)}")

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
            return response, None
        except tweepy.errors.TooManyRequests:
            wait = RATE_LIMIT_WAIT_SECONDS * (attempt + 1)
            log.warning(f"  レートリミット超過。{wait}秒後にリトライ（{attempt + 1}/3）")
            time.sleep(wait)
        except tweepy.errors.TwitterServerError as e:
            log.warning(f"  X サーバーエラー: {e}。15秒後にリトライ（{attempt + 1}/3）")
            time.sleep(15)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            log.error(f"  予期しないエラー: {err}")
            return None, err

    return None, "3回リトライしても失敗"


# ===========================================================================
# メインフロー
# ===========================================================================

def run(
    accounts_path: Path = CONFIG_PATH,
    dry_run: bool = False,
    priority_filter: str | None = None,
) -> None:
    run_start = datetime.now()

    labels = []
    if dry_run:
        labels.append("dry-run")
    if priority_filter:
        labels.append(f"priority={priority_filter}")
    label_str = " ".join(f"[{lab}]" for lab in labels)
    log.info(f"=== X Post Collector 開始 {label_str} ===")

    # ----- アカウント設定読み込み -----
    if not accounts_path.exists():
        log.error(f"アカウント設定ファイルが見つかりません: {accounts_path}")
        sys.exit(1)

    raw_data = json.loads(accounts_path.read_text(encoding="utf-8"))
    all_accounts = normalize_accounts(raw_data.get("accounts", []))
    if not all_accounts:
        log.error("accounts.json に有効なアカウントが1件もありません。")
        sys.exit(1)

    accounts = filter_by_priority(all_accounts, priority_filter)
    if not accounts:
        log.warning(f"優先度 '{priority_filter}' に該当するアカウントがありません。")
        return

    usernames = [a["username"] for a in accounts]
    log.info(
        f"監視アカウント: {len(usernames)} 件 "
        f"(全 {len(all_accounts)} 件中 / 優先度フィルタ: {priority_filter or 'なし'})"
    )

    # ----- バッチ分割 -----
    batches = split_into_batches(usernames)
    max_batch_size = max(len(b) for b in batches)
    log.info(
        f"バッチ数: {len(batches)} 個（1バッチあたり最大 {max_batch_size} アカウント）"
    )
    log.info(f"1日2回実行時の推定リクエスト数: {len(batches) * 2} 件/日")

    # ----- dry-run はここで終了 -----
    if dry_run:
        log.info("[dry-run] API は呼びません。バッチ構造の確認のみ行いました。")
        for i, batch in enumerate(batches, 1):
            query = build_query(batch)
            log.info(f"  バッチ {i:03d}: {len(batch)} アカウント / クエリ長 {len(query)} 文字")
        log.info(
            f"[dry-run] 完了。本番実行時の保存先: "
            f"data/{run_start.strftime('%Y-%m-%d')}/run_{run_start.strftime('%H-%M-%S')}/"
        )
        return

    # ----- 本番実行 -----
    client = get_client()
    since_ids = load_since_ids()
    new_since_ids: dict[str, str] = {}

    run_dir        = DATA_DIR / run_start.strftime("%Y-%m-%d") / f"run_{run_start.strftime('%H-%M-%S')}"
    raw_dir        = run_dir / "raw"
    tweets_dir     = run_dir / "tweets"
    by_account_dir = run_dir / "by_account"
    raw_dir.mkdir(parents=True, exist_ok=True)
    tweets_dir.mkdir(parents=True, exist_ok=True)
    by_account_dir.mkdir(parents=True, exist_ok=True)

    total_tweets   = 0
    failed_batches = 0
    batch_records: list[dict] = []
    by_account_buffer: dict[str, list[dict]] = {}

    for i, batch in enumerate(batches, 1):
        batch_key = f"batch_{i:03d}"
        since_id = since_ids.get(batch_key)

        response, err = fetch_batch(client, batch, i, since_id)

        if err is not None:
            failed_batches += 1
            batch_records.append({
                "index":       i,
                "accounts":    batch,
                "status":      "failed",
                "error":       err,
                "tweet_count": 0,
            })
            time.sleep(1)
            continue

        # ---- raw 保存（API レスポンスをほぼそのまま）----
        raw = serialize_response(response)
        raw_path = raw_dir / f"{batch_key}.json"
        atomic_write_json(raw_path, {
            "fetched_at":   run_start.isoformat(),
            "batch_index":  i,
            "accounts":     batch,
            "raw_response": raw,
        })

        # ---- 加工版保存（後段が使いやすい形）----
        tweets = parse_tweets(response)
        tweets_path = tweets_dir / f"{batch_key}.json"
        atomic_write_json(tweets_path, {
            "fetched_at":  run_start.isoformat(),
            "batch_index": i,
            "accounts":    batch,
            "tweet_count": len(tweets),
            "tweets":      tweets,
        })

        # ---- アカウント別バッファに振り分け ----
        for tw in tweets:
            by_account_buffer.setdefault(tw["username"], []).append(tw)

        # ---- since_id 更新 ----
        if response.meta and response.meta.get("newest_id"):
            new_since_ids[batch_key] = str(response.meta["newest_id"])

        total_tweets += len(tweets)
        batch_records.append({
            "index":       i,
            "accounts":    batch,
            "status":      "ok",
            "tweet_count": len(tweets),
            "raw_path":    str(raw_path.relative_to(run_dir)),
            "tweets_path": str(tweets_path.relative_to(run_dir)),
        })

        log.info(f"  バッチ {i:03d}: {len(tweets)} 件取得")
        time.sleep(1)

    # ----- アカウント別ファイル書き出し -----
    by_account_index: dict[str, dict] = {}
    for username, tweets in by_account_buffer.items():
        path = by_account_dir / f"{username}.json"
        atomic_write_json(path, {
            "username":    username,
            "fetched_at":  run_start.isoformat(),
            "tweet_count": len(tweets),
            "tweets":      tweets,
        })
        by_account_index[username] = {
            "count": len(tweets),
            "path":  str(path.relative_to(run_dir)),
        }

    # ----- since_ids 更新（成功分のみ）-----
    since_ids.update(new_since_ids)
    save_since_ids(since_ids)

    # ----- マニフェスト書き出し -----
    manifest = {
        "run_at":          run_start.isoformat(),
        "priority_filter": priority_filter,
        "account_count":   len(accounts),
        "batch_count":     len(batches),
        "failed_batches":  failed_batches,
        "total_tweets":    total_tweets,
        "elapsed_seconds": round((datetime.now() - run_start).total_seconds(), 1),
        "by_account":      by_account_index,
        "batches":         batch_records,
    }
    atomic_write_json(run_dir / "manifest.json", manifest)

    log.info(
        f"=== 完了 === 取得 {total_tweets} 件 / "
        f"アカウント {len(by_account_index)} 件 / "
        f"失敗バッチ {failed_batches} / "
        f"経過 {manifest['elapsed_seconds']}秒"
    )


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="X Post Collector")
    parser.add_argument("--dry-run", action="store_true", help="API を呼ばずに動作確認のみ行う")
    parser.add_argument("--accounts", type=Path, default=CONFIG_PATH, help="アカウント設定ファイルのパス")
    parser.add_argument(
        "--priority",
        choices=["high", "normal", "low"],
        default=None,
        help="この優先度のアカウントだけ取得する（指定なしで全件）",
    )
    args = parser.parse_args()

    run(
        accounts_path=args.accounts,
        dry_run=args.dry_run,
        priority_filter=args.priority,
    )
