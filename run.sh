#!/bin/bash
# run.sh — collector.py を1コマンドで実行する
# 使い方:
#   bash run.sh                # 全アカウント取得
#   bash run.sh --dry-run      # APIを呼ばず動作確認のみ
#   bash run.sh --priority high # 高優先度アカウントだけ取得

set -e

cd "$(dirname "$0")"

# venv が無ければ案内
if [ ! -d venv ]; then
    echo "❌ venv が見つかりません。先に setup.sh を実行してください："
    echo "   bash setup.sh"
    exit 1
fi

# .env が無ければ案内
if [ ! -f .env ]; then
    echo "❌ .env が見つかりません。setup.sh を実行するか、.env.example をコピーしてください："
    echo "   cp .env.example .env"
    exit 1
fi

# Bearer Token がプレースホルダーのままなら警告
if grep -q "^X_BEARER_TOKEN=ここに" .env 2>/dev/null; then
    echo "⚠️  .env の X_BEARER_TOKEN がプレースホルダーのままです。"
    echo "    https://console.x.com でトークンを生成して .env に貼ってください。"
    exit 1
fi

# collector.py に引数をすべて渡す
exec venv/bin/python3 collector.py "$@"
