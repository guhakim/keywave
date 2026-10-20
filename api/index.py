# Vercel 서버리스 함수: 모든 주소를 KEYWAVE 서버(server.py)의 Handler가 그대로 처리합니다.
# 화면 파일도 이 함수가 보내서, 비밀번호(KEYWAVE_PASSWORD) 없이는 아무것도 열리지 않아요.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KEYWAVE_DEPLOY", "1")

from server import Handler as handler  # noqa: E402,F401  (Vercel은 'handler'라는 이름을 찾습니다)
