"""
X Post Collector
================

X (旧Twitter) の指定アカウント群の最新ポストを定期取得・保存するツール。

【依頼仕様への対応】
- 1000アカウントを OR クエリでバッチ化し、リクエスト数を最小化
- raw API レスポンスを raw/ 配下にそのまま保存（後段の再処理用）
- 加工版を tweets/ 配下に保存（すぐに使える形式）
- アカウント別ビューを by_account/ 配下に生成
- 1実行ごとに manifest.json を生成（取得件数・推定コスト等）
- 設定ファイル（config/accounts.json）でアカウント追加・停止が可能
- 優先度（high/normal/low）でフィルタ実行が可能
- 取りこぼし防止: next_token ページネーション対応
- コスト最適化:
  - ユーザー情報の事前キャッシュ（毎回の expansion を避ける）
  - メディア取得を ON/OFF で切替可能
- アトミック書き込み（temp + rename）でデータ破損防止
- レートリミット時の待機・リトライ
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

SCRIPT_DIR        = Path(__file__).parent
CONFIG_PATH       = SCRIPT_DIR / "config" / "accounts.json"
DATA_DIR          = SCRIPT_DIR / "data"
LOG_DIR           = SCRIPT_DIR / "logs"
SINCE_ID_PATH     = DATA_DIR / "since_ids.json"
USERS_CACHE_PATH  = DATA_DIR / "users_cache.json"

MAX_QUERY_LENGTH        = 480
RATE_LIMIT_WAIT_SECONDS = 60
MAX_RESULTS_PER_BATCH   = 100
MAX_PAGES_PER_BATCH     = 10   # ページネーションの安全弁
USERS_LOOKUP_CHUNK      = 100  # GET /2/users/by の1リクエスト上限
VALID_PRIORITIES        = {"high", "normal", "low"}

# X API Pay-Per-Use 単価（2026-04 時点）
COST_PER_POST_READ  = 0.005
COST_PER_USER_READ  = 0.010
COST_PER_MEDIA_READ = 0.005

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

def _normalize_username(name: str) -> str:
    """username を正規化する。先頭の @ を除去し、小文字化する。

    X 上で username は大文字小文字を区別しないため、accounts.json に
    "@NHK_News" / "nhk_news" / "NHK_news" などが混在していても重複扱いされず
    バッチ・by_account ファイル名で表記揺れが起きないようにする。
    """
    return name.lstrip("@").lower()


def normalize_accounts(raw_accounts: list) -> list[dict]:
    """
    accounts.json の中身を正規形 [{"username": ..., "priority": ...}, ...] に変換する。
    旧形式（"username" 文字列）も新形式（dict）も受け入れる。
    username は先頭の @ 除去・小文字化で正規化される。
    """
    normalized = []
    for item in raw_accounts:
        if isinstance(item, str):
            name = _normalize_username(item)
            if not name:
                continue
            normalized.append({"username": name, "priority": "normal"})
        elif isinstance(item, dict):
            raw_name = item.get("username")
            if not raw_name:
                continue
            name = _normalize_username(raw_name)
            if not name:
                continue
            priority = item.get("priority", "normal")
            if priority not in VALID_PRIORITIES:
                priority = "normal"
            normalized.append({"username": name, "priority": priority})
    return normalized


def make_since_id_key(priority_filter: str | None, batch_index: int) -> str:
    """since_ids.json 保存用のキーを生成する。

    優先度別の並列実行（high 6h / normal 12h / low 24h など）で
    別フィルタの since_id が同じキーを上書き合戦しないよう、
    キーに priority_filter を含める。フィルタなし実行は "all" として扱う。
    """
    prefix = priority_filter if priority_filter else "all"
    return f"{prefix}_batch_{batch_index:03d}"


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
    """temp に書いてから rename で原子的に置き換える。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, data) -> None:
    """JSON データをアトミックに書き出す。"""
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def estimate_cost_usd(
    post_reads: int,
    user_reads: int,
    media_reads: int,
) -> dict:
    """
    取得件数からX API課金額（USD）を見積もる純粋関数。
    24h dedup は考慮しない（同一実行内では発生しない前提）。
    """
    posts = round(post_reads * COST_PER_POST_READ, 4)
    users = round(user_reads * COST_PER_USER_READ, 4)
    media = round(media_reads * COST_PER_MEDIA_READ, 4)
    return {
        "post_reads":     post_reads,
        "user_reads":     user_reads,
        "media_reads":    media_reads,
        "posts_usd":      posts,
        "users_usd":      users,
        "media_usd":      media,
        "total_usd":      round(posts + users + media, 4),
    }


# ---------------------------------------------------------------------------
# tweepy レスポンスのシリアライズ
# ---------------------------------------------------------------------------

def serialize_response(response) -> dict:
    """tweepy.Response を raw JSON 相当の dict に変換する。"""
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


def parse_tweets(
    response,
    user_id_to_username: dict[str, str] | None = None,
    include_media: bool = True,
) -> list[dict]:
    """response から、後段で扱いやすい形に整形した tweet リストを返す。

    user_id_to_username が渡された場合、それを優先して username を解決する
    （expansions=author_id を使わない場合のフォールバック）。
    include_media=False の場合、media フィールドは常に空リストになる。
    """
    if not response.data:
        return []

    # response.includes の users で解決（expansion を使った場合）
    users: dict[str, str] = {}
    if response.includes and "users" in response.includes:
        for user in response.includes["users"]:
            users[str(user.id)] = user.username
    # キャッシュ由来のマッピングを優先（または補完）
    if user_id_to_username:
        for uid, uname in user_id_to_username.items():
            users[str(uid)] = uname

    media_map: dict[str, dict] = {}
    if include_media and response.includes and "media" in response.includes:
        for media in response.includes["media"]:
            url = getattr(media, "url", None) or getattr(media, "preview_image_url", None)
            media_map[media.media_key] = {"type": media.type, "url": url}

    tweets = []
    for tweet in response.data:
        if include_media:
            attachments = getattr(tweet, "attachments", None) or {}
            media_keys = attachments.get("media_keys", []) if isinstance(attachments, dict) else []
            media_urls = [media_map[k] for k in media_keys if k in media_map]
        else:
            media_urls = []

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


# ---------------------------------------------------------------------------
# ユーザーキャッシュ：username ↔ user_id のマッピング
# 一度取得すれば永続的に再利用できるため、ユーザー情報の課金を最小化する
# ---------------------------------------------------------------------------

def load_users_cache() -> dict[str, str]:
    """username -> user_id のマップを返す。"""
    if not USERS_CACHE_PATH.exists():
        return {}
    try:
        cache = json.loads(USERS_CACHE_PATH.read_text(encoding="utf-8"))
        return {
            k: (v["id"] if isinstance(v, dict) else v)
            for k, v in cache.get("users", {}).items()
        }
    except Exception as e:
        log.warning(f"users_cache.json の読み込みに失敗: {e}")
        return {}


def save_users_cache(cache: dict[str, str]) -> None:
    payload = {
        "version": 1,
        "saved_at": datetime.now().isoformat(),
        "users": {
            username: {"id": user_id}
            for username, user_id in sorted(cache.items())
        },
    }
    atomic_write_json(USERS_CACHE_PATH, payload)


def ensure_users_cached(client, usernames: list[str]) -> tuple[dict[str, str], int]:
    """
    指定ユーザーが全てキャッシュにあることを保証する。なければ API で取得して追加する。
    Returns: (username->user_id マップ, このコールで API から新規取得した数)
    """
    cache = load_users_cache()
    missing = [u for u in usernames if u not in cache]
    if not missing:
        return cache, 0

    log.info(f"  ユーザーキャッシュを更新します: 新規 {len(missing)} 件")

    fetched = 0
    for chunk_start in range(0, len(missing), USERS_LOOKUP_CHUNK):
        chunk = missing[chunk_start:chunk_start + USERS_LOOKUP_CHUNK]
        for attempt in range(3):
            try:
                response = client.get_users(usernames=chunk)
                break
            except tweepy.errors.TooManyRequests:
                wait = RATE_LIMIT_WAIT_SECONDS * (attempt + 1)
                log.warning(f"  user lookup レートリミット。{wait}秒後にリトライ（{attempt + 1}/3）")
                time.sleep(wait)
            except Exception as e:
                log.error(f"  user lookup でエラー: {e}")
                response = None
                break
        if response is None or not response.data:
            log.warning(f"  ユーザー取得失敗 or 結果なし: chunk={chunk}")
            continue
        for user in response.data:
            cache[user.username] = str(user.id)
            fetched += 1
        # APIエラーで取れなかった分も後で再試行できるように、見つからなかったユーザーも記録
        found_usernames = {user.username for user in response.data}
        for not_found in set(chunk) - found_usernames:
            log.warning(f"  ユーザーが見つかりません（削除/凍結の可能性）: {not_found}")
        time.sleep(0.5)

    save_users_cache(cache)
    log.info(f"  ユーザーキャッシュ更新完了: 追加 {fetched} 件 / 累計 {len(cache)} 件")
    return cache, fetched


def get_client() -> tweepy.Client:
    load_dotenv(SCRIPT_DIR / ".env")
    bearer = os.getenv("X_BEARER_TOKEN")
    if not bearer or bearer.startswith("ここに"):
        raise EnvironmentError(
            ".env に X_BEARER_TOKEN が設定されていません。\n"
            ".env.example をコピーして .env を作成し、Bearer Token を設定してください。"
        )
    return tweepy.Client(bearer_token=bearer, wait_on_rate_limit=False)


# ---------------------------------------------------------------------------
# 1ページ取得（リトライ付き）
# ---------------------------------------------------------------------------

def _call_search(
    client,
    query: str,
    since_id: str | None,
    pagination_token: str | None,
    include_media: bool,
):
    """
    search_recent_tweets を1回呼ぶ。リトライは外側で。
    Returns: (response, error_message or None)
    """
    tweet_fields = ["created_at", "author_id", "text"]
    if include_media:
        tweet_fields.append("attachments")

    expansions = []
    media_fields = None
    if include_media:
        expansions.append("attachments.media_keys")
        media_fields = ["url", "preview_image_url", "type"]

    kwargs = {
        "query":         query,
        "max_results":   MAX_RESULTS_PER_BATCH,
        "since_id":      since_id,
        "tweet_fields":  tweet_fields,
    }
    if expansions:
        kwargs["expansions"] = expansions
    if media_fields:
        kwargs["media_fields"] = media_fields
    if pagination_token:
        kwargs["next_token"] = pagination_token

    try:
        response = client.search_recent_tweets(**kwargs)
        return response, None
    except tweepy.errors.TooManyRequests:
        return None, "TooManyRequests"
    except tweepy.errors.TwitterServerError as e:
        return None, f"TwitterServerError: {e}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def fetch_batch_with_pagination(
    client,
    batch: list[str],
    batch_index: int,
    since_id: str | None,
    include_media: bool,
    user_id_to_username: dict[str, str],
):
    """
    1バッチ分を next_token でページネーションしながら取得する。

    Returns:
        (raw_responses_list, tweets_list, media_count, error or None)
    """
    query = build_query(batch)
    log.info(f"  バッチ {batch_index:03d}: {len(batch)} アカウント / クエリ長 {len(query)}")

    raw_responses: list[dict] = []
    all_tweets: list[dict] = []
    media_count = 0
    pagination_token: str | None = None
    pages_fetched = 0

    for page in range(1, MAX_PAGES_PER_BATCH + 1):
        # リトライループ
        for attempt in range(3):
            response, err = _call_search(client, query, since_id, pagination_token, include_media)
            if response is not None:
                break
            if err == "TooManyRequests":
                wait = RATE_LIMIT_WAIT_SECONDS * (attempt + 1)
                log.warning(f"  rate limit. {wait}秒後にリトライ（{attempt + 1}/3）")
                time.sleep(wait)
            else:
                log.warning(f"  {err}。15秒後にリトライ（{attempt + 1}/3）")
                time.sleep(15)
        else:
            return raw_responses, all_tweets, media_count, f"3回リトライしても失敗（page {page}）"

        pages_fetched += 1
        raw_responses.append(serialize_response(response))

        page_tweets = parse_tweets(response, user_id_to_username, include_media=include_media)
        all_tweets.extend(page_tweets)
        if include_media:
            media_count += sum(len(t.get("media", [])) for t in page_tweets)

        # 次ページがあるか
        next_token = response.meta.get("next_token") if response.meta else None
        if not next_token:
            break
        pagination_token = next_token
        time.sleep(0.5)
    else:
        log.warning(f"  バッチ {batch_index:03d}: 最大ページ数({MAX_PAGES_PER_BATCH})に到達。残りはスキップ")

    log.info(f"  バッチ {batch_index:03d}: {len(all_tweets)} 件取得（{pages_fetched} ページ）")
    return raw_responses, all_tweets, media_count, None


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
    settings = raw_data.get("settings", {}) or {}
    include_media = bool(settings.get("include_media", True))

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
        f"(全 {len(all_accounts)} 件中 / 優先度フィルタ: {priority_filter or 'なし'} / "
        f"media: {'ON' if include_media else 'OFF'})"
    )

    # ----- バッチ分割 -----
    batches = split_into_batches(usernames)
    max_batch_size = max(len(b) for b in batches)
    log.info(
        f"バッチ数: {len(batches)} 個（1バッチ最大 {max_batch_size} アカウント）"
    )
    log.info(f"1日2回実行時の推定リクエスト数: 最低 {len(batches) * 2} 件/日（ページネーション分は加算）")

    # ----- dry-run はここで終了 -----
    if dry_run:
        log.info("[dry-run] API は呼びません。バッチ構造の確認のみ。")
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

    # ユーザーキャッシュを更新（不足分を取得）
    user_cache, new_user_lookups = ensure_users_cached(client, usernames)
    user_id_to_username = {uid: uname for uname, uid in user_cache.items()}

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
    total_media    = 0
    failed_batches = 0
    batch_records: list[dict] = []
    by_account_buffer: dict[str, list[dict]] = {}

    for i, batch in enumerate(batches, 1):
        batch_filename = f"batch_{i:03d}"
        since_key = make_since_id_key(priority_filter, i)
        since_id = since_ids.get(since_key)

        raw_responses, tweets, media_count, err = fetch_batch_with_pagination(
            client, batch, i, since_id,
            include_media=include_media,
            user_id_to_username=user_id_to_username,
        )

        # 「投稿取得に成功したものは raw として残る」要件のため、
        # エラーが出ても、それまでに取得できた raw / tweets は必ず保存する。
        raw_path: Path | None = None
        tweets_path: Path | None = None
        if raw_responses:
            raw_path = raw_dir / f"{batch_filename}.json"
            atomic_write_json(raw_path, {
                "fetched_at":    run_start.isoformat(),
                "batch_index":   i,
                "accounts":      batch,
                "page_count":    len(raw_responses),
                "raw_responses": raw_responses,
            })

            tweets_path = tweets_dir / f"{batch_filename}.json"
            atomic_write_json(tweets_path, {
                "fetched_at":  run_start.isoformat(),
                "batch_index": i,
                "accounts":    batch,
                "tweet_count": len(tweets),
                "page_count":  len(raw_responses),
                "tweets":      tweets,
            })

            for tw in tweets:
                by_account_buffer.setdefault(tw["username"], []).append(tw)

            first_meta = raw_responses[0].get("meta") or {}
            newest_id = first_meta.get("newest_id")
            if newest_id:
                new_since_ids[since_key] = str(newest_id)

            total_tweets += len(tweets)
            total_media  += media_count

        # ステータス判定: 完全成功 / 部分成功（一部ページ失敗） / 完全失敗
        if err is None:
            status = "ok"
        elif raw_responses:
            status = "partial"
            failed_batches += 1
        else:
            status = "failed"
            failed_batches += 1

        record = {
            "index":       i,
            "accounts":    batch,
            "status":      status,
            "tweet_count": len(tweets),
            "media_count": media_count,
            "page_count":  len(raw_responses),
        }
        if err is not None:
            record["error"] = err
        if raw_path is not None:
            record["raw_path"] = str(raw_path.relative_to(run_dir))
        if tweets_path is not None:
            record["tweets_path"] = str(tweets_path.relative_to(run_dir))
        batch_records.append(record)

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

    # ----- since_ids 更新 -----
    since_ids.update(new_since_ids)
    save_since_ids(since_ids)

    # ----- コスト推定 -----
    cost = estimate_cost_usd(
        post_reads=total_tweets,
        user_reads=new_user_lookups,
        media_reads=total_media,
    )

    # ----- マニフェスト書き出し -----
    manifest = {
        "run_at":          run_start.isoformat(),
        "priority_filter": priority_filter,
        "include_media":   include_media,
        "account_count":   len(accounts),
        "batch_count":     len(batches),
        "failed_batches":  failed_batches,
        "total_tweets":    total_tweets,
        "total_media":     total_media,
        "elapsed_seconds": round((datetime.now() - run_start).total_seconds(), 1),
        "estimated_cost":  cost,
        "by_account":      by_account_index,
        "batches":         batch_records,
    }
    atomic_write_json(run_dir / "manifest.json", manifest)

    log.info(
        f"=== 完了 === 取得 {total_tweets} 件 / "
        f"アカウント {len(by_account_index)} 件 / "
        f"失敗バッチ {failed_batches} / "
        f"推定課金額 ${cost['total_usd']} / "
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
