#!/bin/bash
# setup.sh — 初回セットアップを1コマンドで完了させる
# 使い方: bash setup.sh

set -e

cd "$(dirname "$0")"

echo "==> X Post Collector セットアップを開始します"
echo ""

# Python 3.11+ が入っているか確認
if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ python3 が見つかりません。Python 3.11 以上をインストールしてください。"
    echo "   macOS: brew install python@3.11"
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "✅ Python ${PY_VERSION} を検出しました"

# 仮想環境の作成
if [ ! -d venv ]; then
    echo "==> 仮想環境（venv）を作成します..."
    python3 -m venv venv
    echo "✅ venv 作成完了"
else
    echo "✅ venv は既に存在します"
fi

# 必要パッケージのインストール
echo "==> 必要パッケージをインストールします..."
venv/bin/pip install --upgrade pip --quiet
venv/bin/pip install -r requirements.txt --quiet
echo "✅ パッケージインストール完了"

# .env の準備
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "📝 .env を作成しました。次の手順で Bearer Token を設定してください："
    echo "   1. https://console.x.com を開く"
    echo "   2. アプリの「Keys & Tokens」→「ペアラートークン」を生成"
    echo "   3. .env を開いて X_BEARER_TOKEN= の右に貼り付け"
    echo ""
else
    echo "✅ .env は既に存在します"
fi

echo ""
echo "==> セットアップ完了！次に進む場合は："
echo "   - 動作確認:  bash run.sh --dry-run"
echo "   - 本番実行:  bash run.sh"
echo "   - 状況表示:  bash status.sh"
echo ""
