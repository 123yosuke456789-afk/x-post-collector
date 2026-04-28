#!/bin/bash
# status.sh — 直近の取得状況を人間向けに表示する（status.py のラッパー）
# 使い方: bash status.sh
#
# Windows の cmd / PowerShell の方は次のコマンドをお使いください：
#   python status.py

set -e

cd "$(dirname "$0")"

if [ -x venv/bin/python3 ]; then
    exec venv/bin/python3 status.py
else
    exec python3 status.py
fi
