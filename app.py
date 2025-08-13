import os
import time
import logging
from typing import Any, Dict, Optional

import requests
from flask import Flask, request, jsonify, Response, g

# ======================
# 기본 설정 / 로깅
# ======================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("kakao-skill")

app = Flask(__name__)
# 혹시 큰 요청이 와도 방어 (1MB)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024

# ======================
# 환경 변수 (필요 시만 사용)
# ======================
API_KEY: Optional[str] = os.getenv("CHATLING_API_KEY")
CHATLING_URL: str = os.getenv("CHATLING_URL", "https://api.chatling.ai/v1/respond")
CHATLING_TIMEOUT: float = float(os.getenv("CHATLING_TIMEOUT", "1.8"))
# 문서에 따라 필요할 수 있는 값들(없어도 동작)
CHATLING_BOT_ID: Optional[str] = os.getenv("CHATLING_BOT_ID")
CHATLING_SOURCE_ID: Optional[str] = os.getenv("CHATLING_SOURCE_ID")

# 최근 Chatling 호출 상태(진단용)
last_chatling: Dict[str, Any] = {
    "ok": False,
    "status": None,
    "body_snippet": None,
    "error": None,
    "url": CHATLING_URL,
}

# ======================
# 공용: 카카오 v2.0 텍스트 응답
# ======================
def kakao_text(text: str):
    return jsonify({
        "version": "2.0",
        "template": {
            "outputs": [
                {"simpleText": {"text": text or ""}}
            ]
        }
    })

# ======================
# 간단 액세스 로그
# ======================
@app.before_request
def _t0():
    g.t0 = time.time()

@app.after_request
def _after(resp: Response):
    try:
        took_ms = int((time.time() - getattr(g, "t0", time.time())) * 1000)
        log.info("path=%s method=%s status=%s took_ms=%s",
                 request.path, request.method, resp.status_code, took_ms)
    except Exception:
        pass
    return resp

# ======================
# 전역 예외: 항상 200 + 안전 메시지 (카카오 타임아웃 보호)
# ======================
@app.errorhandler(Exception)
def _err(e):
    log.exception("Unhandled error on %s", request.path)
    return kakao_text("일시적 오류가 있었지만 연결은 유지되었습니다."), 200

# ======================
# 헬스체크 (플랫폼/프록시 호환 위해 / 와 /healthz 둘 다 OK)
# ======================
@app.get("/healthz")
def healthz():
    return Response(b"ok", 200, {"Content-Type": "text/plain"})

@app.get("/")
def root_ok():
    return Response(b"ok", 200, {"Content-Type": "text/plain"})

# ======================
# Chatling 호출
# - 키/입력 없거나 실패/지연 시 None 반환 → 즉시 대체응답
# - 엔드포인트/바디 스키마는 환경에 맞게 유연 처리
# ======================
def _first_string(*candidates) -> Optional[str]:
    for v in candidates:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None

def _extract_answer(js: Dict[str, Any]) -> Optional[str]:
    """
    다양한 응답 스키마를 관대하게 지원:
    - { "answer": "..." }
    - { "response": "..." }
    - { "message": "..." } / { "output": "..." }
    - { "data": { ... 동일 키들 ... } }
    - OpenAI 스타일: { "choices":[{"message":{"content":"..."}}] }
    """
    ans = _first_string(
        js.get("answer"),
        js.get("response"),
        js.get("message"),
        js.get("output"),
    )
    if ans:
        return ans

    data = js.get("data")
    if isinstance(data, dict):
        ans = _first_string(
            data.get("answer"),
            data.get("response"),
            data.get("message"),
            data.get("output"),
        )
        if ans:
            return ans

    # OpenAI-ish
    try:
        return js["choices"][0]["message"]["content"].strip()
    exc
