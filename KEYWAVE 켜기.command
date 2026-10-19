#!/bin/bash
# 더블클릭하면 KEYWAVE 서버를 켜고 브라우저로 엽니다. 끄려면 이 창을 닫으세요.
cd "$(dirname "$0")" || exit 1

if lsof -ti tcp:8765 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "KEYWAVE가 이미 켜져 있어요. 브라우저로 열게요."
  open "http://localhost:8765"
  exit 0
fi

(sleep 1.5 && open "http://localhost:8765") &
echo "KEYWAVE를 켜는 중이에요… (끄려면 이 창을 닫으세요)"
# AI 인사이트용 패키지가 든 .venv가 있으면 그걸로, 없으면 기본 python3로 켭니다
PY=".venv/bin/python"; [ -x "$PY" ] || PY="python3"
"$PY" server.py
