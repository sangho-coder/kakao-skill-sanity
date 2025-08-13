import os
import time
import logging
from typing import Any, Dict, Optional

import requests
from flask import Flask, request, jsonify, Response, g

# ===== 로깅 =====
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("kakao-skill")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
app.config.update(JSON_AS_ASCII=False)  # /diag 한글 깨짐 방지

# ===== 환경변수 =====
API_KEY: Optional[str] = os.getenv("CHATLING_API_KEY")
CHATLING_URL: str = os.getenv("CHATLING_URL", "https://api.chatling.ai/v1/respond")
CHATLING_TIMEOUT: float = float(os.getenv("CHATLING_TIMEOUT", "1.8"))
CHATLING_BOT_ID: Optional[str] = os.getenv("CHATLING_BOT_ID")
CHATLING_SOURCE_ID: Optional[str] = os.getenv("CHATLING_SOURCE_ID")

# v2가 보통 'query'를, v1이 'message'를 쓰는 경우가 많음.
# 직접 지정하고 싶으면 CHATLING_BODY_KEY 로 덮어씌우기.
_auto_body_key = "query" if "/v2/" in CHATLING_URL else "message"
CHATLING_BODY_KEY: str = os.getenv("CHATLING_BODY_KEY", _auto_body_key)

last_chatling: Dict[str, Any] = {"ok": False, "status": None, "body_snippet": None, "error": None, "url": CHATLING_URL}
last_request: Dict[str, Any] = {"utter": None, "source": None, "raw_usrtext": None, "raw_utterance": None}

# ===== 공용 응답 =====
def kakao_text(text: str):
    return jsonify({"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text or ""}}]}})

# ===== 액세스 로그 =====
@app.before_request
def _t0():
    g.t0 = time.time()

@app.after_request
def _after(resp: Response):
    try:
        took_ms = int((time.time() - getattr(g, "t0", time.time())) * 1000)
        log.info("path=%s method=%s status=%s took_ms=%s", request.path, request.method, resp.status_code, took_ms)
    except Exception:
        pass
    return resp

@app.errorhandler(Exception)
def _err(e):
    log.exception("Unhandled error on %s", request.path)
    return kakao_text("일시적 오류가 있었지만 연결은 유지되었습니다."), 200

# ===== 헬스/루트/진단 =====
@app.get("/healthz")
def healthz():
    return Response(b"ok", 200, {"Content-Type": "text/plain"})

@app.get("/")
def root_ok():
    return Response(b"ok", 200, {"Content-Type": "text/plain"})

@app.get("/diag")
def diag():
    payload = {
        "api_key_set": bool(API_KEY),
        "chatling_url": CHATLING_URL,
        "body_key": CHATLING_BODY_KEY,
        "timeout_s": CHATLING_TIMEOUT,
        "last_chatling": l_
