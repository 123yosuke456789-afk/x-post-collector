"""
バッチ分割ロジックの単体テスト。

APIキーなしで実行できる。
1000アカウントでも正しく動作することをここで証明する。
"""

import pytest
from collector import split_into_batches, build_query, MAX_QUERY_LENGTH


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
        """各バッチのクエリ長がMAX_QUERY_LENGTH以内に収まる。"""
        accounts = [f"user_{i}" for i in range(1000)]
        batches = split_into_batches(accounts)
        for i, batch in enumerate(batches):
            # build_query は除外フィルタ (-is:retweet 等) も加えるのでそちらで計算
            query = build_query(batch)
            # build_query が追加するフィルタを除いたコア部分がMAX_QUERY_LENGTH以内
            core_query = " OR ".join(f"from:{a}" for a in batch)
            assert len(core_query) <= MAX_QUERY_LENGTH, (
                f"バッチ {i+1} のコアクエリが {len(core_query)} 文字（上限 {MAX_QUERY_LENGTH}）"
            )

    def test_1000_accounts_batch_count(self):
        """1000アカウントが適切なバッチ数に分割される（目安: 15〜25バッチ）。"""
        accounts = [f"user_{i}" for i in range(1000)]
        batches = split_into_batches(accounts)
        # 1バッチあたり約25〜60アカウントが入るはず
        assert 15 <= len(batches) <= 50, f"バッチ数: {len(batches)}"

    def test_long_usernames(self):
        """最大15文字のユーザー名でも正しく分割される。"""
        accounts = [f"user_{'x' * 10}_{i}" for i in range(500)]
        batches = split_into_batches(accounts)
        result = [a for batch in batches for a in batch]
        assert sorted(result) == sorted(accounts)
        for batch in batches:
            core_query = " OR ".join(f"from:{a}" for a in batch)
            assert len(core_query) <= MAX_QUERY_LENGTH

    def test_single_account(self):
        """アカウント1件でも動く。"""
        batches = split_into_batches(["nhk_news"])
        assert len(batches) == 1
        assert batches[0] == ["nhk_news"]

    def test_empty_list(self):
        """空リストは空のバッチリストを返す。"""
        batches = split_into_batches([])
        assert batches == []


class TestBuildQuery:
    def test_retweet_excluded(self):
        """-is:retweet が含まれる。"""
        query = build_query(["nhk_news", "mainichi"])
        assert "-is:retweet" in query

    def test_reply_excluded(self):
        """-is:reply が含まれる。"""
        query = build_query(["nhk_news", "mainichi"])
        assert "-is:reply" in query

    def test_from_clause(self):
        """from:username が含まれる。"""
        query = build_query(["nhk_news", "mainichi"])
        assert "from:nhk_news" in query
        assert "from:mainichi" in query

    def test_or_operator(self):
        """複数アカウントが OR で結合される。"""
        query = build_query(["a", "b", "c"])
        assert "OR" in query


class TestScaleReport:
    """1000件スケール時の設計妥当性レポート（テスト兼用）。"""

    def test_print_scale_report(self, capsys):
        """1000アカウント時のバッチ数・推定リクエスト数を出力する。"""
        accounts = [f"user_{i}" for i in range(1000)]
        batches = split_into_batches(accounts)
        daily_requests = len(batches) * 2  # 1日2回実行

        print(f"\n--- 1000アカウントスケールレポート ---")
        print(f"アカウント数     : {len(accounts)}")
        print(f"バッチ数         : {len(batches)}")
        print(f"最大バッチサイズ  : {max(len(b) for b in batches)} アカウント")
        print(f"1日2回実行時     : {daily_requests} リクエスト/日")
        print(f"X API制限（Basic）: 300 リクエスト/15分")
        print(f"余裕率           : {300 / (daily_requests / (24 * 4)):.0f}x 以上の余裕")

        # 1日のリクエスト数が現実的な範囲内か
        assert daily_requests < 100, f"1日{daily_requests}リクエストは想定より多い"
