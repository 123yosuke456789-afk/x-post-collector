# X Post Collector

X（旧Twitter）で指定したアカウント群の最新ポストを定期取得・保存する Python ツール。

**動作確認済み環境**: macOS 14以上 / Python 3.11以上

---

## 機能

- 設定ファイル（`config/accounts.json`）でアカウントを管理。コード修正なしで追加・削除できる
- X API v2 の Search API を OR クエリで束ねてリクエスト数を最小化
- リポスト・リプライを除外し、純粋な新規ポストのみを取得
- 日付・実行単位でディレクトリを分けた JSON 保存
- レートリミット時の自動待機・リトライ（最大3回）
- 前回取得済みの投稿 ID（`since_id`）を記録し、重複取得を防止
- `--dry-run` で API を呼ばずに動作確認できる
- 実行ログを日付ごとのファイルに記録

---

## 1000アカウント対応の設計

1000アカウントを OR クエリに分割してリクエスト数を最小化している。

| アカウント数 | バッチ数 | 1日2回実行時のリクエスト数 | X API Basic 制限（300件/15分）に対する余裕 |
|---:|---:|---:|---:|
| 100 | 4 | 8 | 3000x |
| 500 | 18 | 36 | 830x |
| **1000** | **36** | **72** | **400x** |

この分割ロジックは単体テストで検証済み（`pytest tests/test_batch.py`）。

```
--- 1000アカウントスケールレポート ---
アカウント数     : 1000
バッチ数         : 36
最大バッチサイズ  : 30 アカウント
1日2回実行時     : 72 リクエスト/日
X API制限（Basic）: 300 リクエスト/15分
余裕率           : 400x 以上の余裕
```

---

## インストール

```bash
git clone https://github.com/YOUR_USERNAME/x-post-collector.git
cd x-post-collector
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## 設定

### 1. Bearer Token の設定

```bash
cp .env.example .env
# .env を編集して X_BEARER_TOKEN を記入
```

X Developer Console（[console.x.com](https://console.x.com)）でアプリの Bearer Token を生成してください。

### 2. 監視アカウントの設定

`config/accounts.json` を編集する。

```json
{
  "accounts": [
    "nhk_news",
    "mainichi",
    "yomiuri_online"
  ]
}
```

アカウントの追加・削除はこのファイルだけを変更すればよく、コード修正は不要。

---

## 使い方

```bash
# 通常実行
python collector.py

# 動作確認（API を呼ばない）
python collector.py --dry-run

# アカウントファイルを指定
python collector.py --accounts config/accounts.json
```

### dry-run の出力例（1000アカウントの場合）

```
[INFO] === X Post Collector 開始 [dry-run] ===
[INFO] 監視アカウント数: 1000 件
[INFO] バッチ数: 36 個（1バッチあたり最大 30 アカウント）
[INFO] 1日2回実行時の推定リクエスト数: 72 件/日
[INFO] [dry-run] API は呼び出しません。バッチ構造の確認のみ行います。
[INFO]   バッチ 001: 30 アカウント / クエリ長 291 文字
[INFO]   バッチ 002: 30 アカウント / クエリ長 296 文字
...（36バッチ）
[INFO] [dry-run] 完了。保存先: data/2026-04-28/run_08-00-00/
```

---

## 保存先の構造

```
data/
└── 2026-04-28/
    └── run_08-00-00/
        ├── batch_001.json   ← バッチごとの raw データ
        ├── batch_002.json
        ├── ...
        └── summary.json     ← 実行サマリ（取得件数・失敗数・経過時間）
```

各 `batch_NNN.json` の中身：

```json
{
  "fetched_at": "2026-04-28T08:00:00",
  "batch_index": 1,
  "accounts": ["nhk_news", "mainichi", ...],
  "tweet_count": 45,
  "tweets": [
    {
      "id": "1234567890",
      "author_id": "123456",
      "username": "nhk_news",
      "text": "...",
      "created_at": "2026-04-28T07:45:00+00:00",
      "media": [{"type": "photo", "url": "https://..."}]
    }
  ]
}
```

---

## 定期実行（launchd）

1日2回（8:00 と 20:00）自動実行する設定例（macOS）:

```xml
<!-- ~/Library/LaunchAgents/com.user.x-post-collector.plist -->
<key>StartCalendarInterval</key>
<array>
  <dict>
    <key>Hour</key><integer>8</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <dict>
    <key>Hour</key><integer>20</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
</array>
```

---

## テスト実行

```bash
pytest tests/test_batch.py -v
```

---

## エラー時の確認方法

```bash
# 当日のログを確認
cat logs/2026-04-28.log

# 実行サマリを確認
cat data/2026-04-28/run_08-00-00/summary.json
```

`summary.json` の `failed_batches` が 0 以外の場合は、ログで該当バッチのエラー内容を確認してください。失敗バッチ以外の取得済みデータは保存されています。
