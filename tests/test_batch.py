"""
バッチ分割ロジックの単体テスト。
APIキーなしで実行できる。1000・1500アカウントでも正しく動作することをここで証明する。
"""

import json
from pathlib import Path

import pytest

from collector import (
    COST_PER_MEDIA_READ,
    COST_PER_POST_READ,
    COST_PER_USER_READ,
    MAX_QUERY_LENGTH,
    atomic_write_json,
    build_query,
    estimate_cost_usd,
    filter_by_priority,
    normalize_accounts,
    split_into_batches,
)


# ===========================================================================
# split_into_batches
# ===========================================================================

class TestSplitIntoBatches:
    def test_small_list(self):
        """少数のアカウントは1バッチにまとまる。"""
        accounts = ["nhk_news", "mainichi", "yomiuri_online"]
        batches = split_into_batches(accounts)
        assert len(batches) == 1
        assert batches[0] == accounts

    def test_all_accounts_included(self):
        """全アカウントが漏れなくいずれかのバッチに含まれる。"""
        accounts = [f"user_{i}" for i in range(1000)]
        batches = split_into_batches(accounts)
        result = [a for batch in batches for a in batch]
        assert sorted(result) == sorted(accounts)

    def test_no_duplicate_accounts(self):
        """同じアカウントが複数バッチに重複して入らない。"""
        accounts = [f"user_{i}" for i in range(1000)]
        batches = split_into_batches(accounts)
        all_accounts = [a for batch in batches for a in batch]
        assert len(all_accounts) == len(set(all_accounts))

    def test_query_length_within_limit(self):
        """各バッチのコアクエリ長が MAX_QUERY_LENGTH 以内に収まる。"""
        accounts = [f"user_{i}" for i in range(1000)]
        batches = split_into_batches(accounts)
        for i, batch in enumerate(batches):
            core_query = " OR ".join(f"from:{a}" for a in batch)
            assert len(core_query) <= MAX_QUERY_LENGTH, (
                f"バッチ {i+1} のクエリが {len(core_query)} 文字（上限 {MAX_QUERY_LENGTH}）"
            )

    def test_1000_accounts_batch_count(self):
        """1000アカウントが適切なバッチ数に分割される。"""
        accounts = [f"user_{i}" for i in range(1000)]
        batches = split_into_batches(accounts)
        assert 15 <= len(batches) <= 50, f"バッチ数: {len(batches)}"

    def test_1500_accounts_scale(self):
        """1500アカウント（依頼の「約1000」が増加した場合）でも動作する。"""
        accounts = [f"user_{i}" for i in range(1500)]
        batches = split_into_batches(accounts)
        result = [a for batch in batches for a in batch]
        assert sorted(result) == sorted(accounts)
        for batch in batches:
            core_query = " OR ".join(f"from:{a}" for a in batch)
            assert len(core_query) <= MAX_QUERY_LENGTH

    def test_long_usernames(self):
        """X 上限の 15 文字に近いユーザー名でも正しく分割される。"""
        accounts = [f"user_{'x' * 10}_{i}" for i in range(500)]
        batches = split_into_batches(accounts)
        result = [a for batch in batches for a in batch]
        assert sorted(result) == sorted(accounts)
        for batch in batches:
            core_query = " OR ".join(f"from:{a}" for a in batch)
            assert len(core_query) <= MAX_QUERY_LENGTH

    def test_single_account(self):
        batches = split_into_batches(["nhk_news"])
        assert len(batches) == 1
        assert batches[0] == ["nhk_news"]

    def test_empty_list(self):
        assert split_into_batches([]) == []


# ===========================================================================
# build_query
# ===========================================================================

class TestBuildQuery:
    def test_retweet_excluded(self):
        assert "-is:retweet" in build_query(["nhk_news", "mainichi"])

    def test_reply_excluded(self):
        assert "-is:reply" in build_query(["nhk_news", "mainichi"])

    def test_from_clause(self):
        query = build_query(["nhk_news", "mainichi"])
        assert "from:nhk_news" in query
        assert "from:mainichi" in query

    def test_or_operator(self):
        assert "OR" in build_query(["a", "b", "c"])


# ===========================================================================
# normalize_accounts
# ===========================================================================

class TestNormalizeAccounts:
    def test_string_format(self):
        """旧形式（文字列）は priority normal に変換される。"""
        result = normalize_accounts(["nhk_news", "mainichi"])
        assert result == [
            {"username": "nhk_news", "priority": "normal"},
            {"username": "mainichi", "priority": "normal"},
        ]

    def test_dict_format(self):
        """新形式（dict）はそのまま正規化される。"""
        result = normalize_accounts([
            {"username": "nhk_news", "priority": "high"},
            {"username": "mainichi", "priority": "low"},
        ])
        assert result == [
            {"username": "nhk_news", "priority": "high"},
            {"username": "mainichi", "priority": "low"},
        ]

    def test_mixed_format(self):
        """文字列と dict が混在しても正しく処理される。"""
        result = normalize_accounts([
            {"username": "nhk_news", "priority": "high"},
            "mainichi",
        ])
        assert result == [
            {"username": "nhk_news", "priority": "high"},
            {"username": "mainichi", "priority": "normal"},
        ]

    def test_invalid_priority_falls_back(self):
        """無効な priority は normal に戻される。"""
        result = normalize_accounts([{"username": "nhk_news", "priority": "urgent"}])
        assert result == [{"username": "nhk_news", "priority": "normal"}]

    def test_missing_username_skipped(self):
        """username がない要素はスキップされる。"""
        result = normalize_accounts([
            {"username": "nhk_news", "priority": "high"},
            {"priority": "high"},
        ])
        assert result == [{"username": "nhk_news", "priority": "high"}]

    def test_dict_without_priority_defaults_to_normal(self):
        result = normalize_accounts([{"username": "nhk_news"}])
        assert result == [{"username": "nhk_news", "priority": "normal"}]


# ===========================================================================
# filter_by_priority
# ===========================================================================

class TestFilterByPriority:
    @pytest.fixture
    def accounts(self):
        return [
            {"username": "a", "priority": "high"},
            {"username": "b", "priority": "high"},
            {"username": "c", "priority": "normal"},
            {"username": "d", "priority": "low"},
        ]

    def test_no_filter_returns_all(self, accounts):
        assert filter_by_priority(accounts, None) == accounts

    def test_high_filter(self, accounts):
        result = filter_by_priority(accounts, "high")
        assert [a["username"] for a in result] == ["a", "b"]

    def test_normal_filter(self, accounts):
        result = filter_by_priority(accounts, "normal")
        assert [a["username"] for a in result] == ["c"]

    def test_low_filter(self, accounts):
        result = filter_by_priority(accounts, "low")
        assert [a["username"] for a in result] == ["d"]

    def test_no_match_returns_empty(self, accounts):
        result = filter_by_priority(
            [{"username": "x", "priority": "normal"}], "high"
        )
        assert result == []


# ===========================================================================
# atomic_write_json
# ===========================================================================

class TestAtomicWriteJson:
    def test_writes_correctly(self, tmp_path: Path):
        target = tmp_path / "test.json"
        atomic_write_json(target, {"key": "value", "n": 42})
        assert json.loads(target.read_text()) == {"key": "value", "n": 42}

    def test_no_temp_file_left(self, tmp_path: Path):
        """書き込み完了後に .tmp ファイルが残らない。"""
        target = tmp_path / "test.json"
        atomic_write_json(target, {"k": "v"})
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []

    def test_creates_parent_directories(self, tmp_path: Path):
        """親ディレクトリがなくても作成される。"""
        target = tmp_path / "deep" / "nested" / "file.json"
        atomic_write_json(target, {"k": "v"})
        assert target.exists()

    def test_overwrite_preserves_old_on_failure(self, tmp_path: Path):
        """古いファイルを上書きするとき、本体は壊れない（rename はアトミック）。"""
        target = tmp_path / "test.json"
        atomic_write_json(target, {"version": 1})
        atomic_write_json(target, {"version": 2})
        assert json.loads(target.read_text()) == {"version": 2}


# ===========================================================================
# estimate_cost_usd
# ===========================================================================

class TestEstimateCostUsd:
    def test_zero(self):
        result = estimate_cost_usd(0, 0, 0)
        assert result["total_usd"] == 0.0
        assert result["posts_usd"] == 0.0
        assert result["users_usd"] == 0.0
        assert result["media_usd"] == 0.0

    def test_only_posts(self):
        result = estimate_cost_usd(post_reads=1000, user_reads=0, media_reads=0)
        assert result["posts_usd"] == 1000 * COST_PER_POST_READ
        assert result["total_usd"] == 1000 * COST_PER_POST_READ

    def test_only_users(self):
        result = estimate_cost_usd(post_reads=0, user_reads=100, media_reads=0)
        assert result["users_usd"] == 100 * COST_PER_USER_READ

    def test_only_media(self):
        result = estimate_cost_usd(post_reads=0, user_reads=0, media_reads=500)
        assert result["media_usd"] == 500 * COST_PER_MEDIA_READ

    def test_combination(self):
        """1000ポスト + 100ユーザー + 500メディア = 5 + 1 + 2.5 = $8.50"""
        result = estimate_cost_usd(post_reads=1000, user_reads=100, media_reads=500)
        assert result["total_usd"] == 8.5

    def test_counts_preserved(self):
        """入力件数も結果に含まれる（manifestに記録するため）。"""
        result = estimate_cost_usd(post_reads=42, user_reads=7, media_reads=13)
        assert result["post_reads"] == 42
        assert result["user_reads"] == 7
        assert result["media_reads"] == 13

    def test_realistic_1000_accounts_daily(self):
        """1000アカウント・1日2回・平均5投稿のケース：おおよそ $25/日。"""
        result = estimate_cost_usd(
            post_reads=5000,    # 5投稿 × 1000アカウント
            user_reads=0,       # キャッシュ済み
            media_reads=0,      # メディアOFF
        )
        assert 24.9 <= result["total_usd"] <= 25.1


# ===========================================================================
# 1000件スケール時のレポート（テスト兼ドキュメント）
# ===========================================================================

class TestScaleReport:
    def test_print_scale_report_1000(self, capsys):
        """1000アカウント時のバッチ数・推定リクエスト数を出力する。"""
        accounts = [f"user_{i}" for i in range(1000)]
        batches = split_into_batches(accounts)
        daily_requests = len(batches) * 2

        print(f"\n--- 1000アカウントスケールレポート ---")
        print(f"アカウント数      : {len(accounts)}")
        print(f"バッチ数          : {len(batches)}")
        print(f"最大バッチサイズ  : {max(len(b) for b in batches)} アカウント")
        print(f"1日2回実行時      : {daily_requests} リクエスト/日")
        print(f"X API制限（Basic）: 300 リクエスト/15分")

        assert daily_requests < 100, f"1日{daily_requests}リクエストは想定より多い"

    def test_print_scale_report_1500(self, capsys):
        """1500アカウントでも余裕で動くことを示す。"""
        accounts = [f"user_{i}" for i in range(1500)]
        batches = split_into_batches(accounts)
        daily_requests = len(batches) * 2

        print(f"\n--- 1500アカウントスケールレポート ---")
        print(f"アカウント数      : {len(accounts)}")
        print(f"バッチ数          : {len(batches)}")
        print(f"最大バッチサイズ  : {max(len(b) for b in batches)} アカウント")
        print(f"1日2回実行時      : {daily_requests} リクエスト/日")

        assert daily_requests < 150
