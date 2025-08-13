import os
import time
import logging
from typing import Optional, Any, Dict

import requests
from flask import Flask, request, jsonify, Response, g

# ===== 로깅 =====
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("kakao-skill")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1_048_576  # 1MB
app.config.update(JSON_AS_ASCII=False)        # /diag 한글 깨짐 방지

# ===== 환경변수 =====
API_KEY: Optional[str] = os.getenv("CHATLING_API_KEY")
CHATLING_URL: str = os.getenv("CHATLING_URL", "https://api.chatling.ai/v1/respond")
CHATLING_TIMEOUT: float = float(os.getenv("CHATLING_TIMEOUT", "1.8"))
CHATLING_BOT_ID: Optional[str] = os.getenv("CHATLING_BOT_ID")
CHATLING_SOURCE_ID: Optional[str] = os.getenv("CHATLING_SOURCE_ID")

# v2면 기본 body key를 'query', 그 외엔 'message'로 자동 설정
CHATLING_BODY_KEY: str = os.getenv(
    "CHATLING_BODY_KEY",
    "query" if "/v2/" in CHATLING_URL else "message",
)

# 진단용 상태 저장
last_chatling: Dict[str, Any] = {
    "ok": False,
    "status": None,
    "body_snippet": None,
    "error": None,
    "url": CHATLING_URL,
}
last_request: Dict[str, Any] = {
    "utter": None,
    "source": None,
    "raw_usrtext": None,
    "raw_utterance": None,
}

# ===== 공용 응답 =====
def kakao_text(text: str):
    return jsonify({
        "version": "2.0",
        "template": {"outputs": [{"simpleText": {"text": text or ""}}]},
    })

# ===== 액세스 로그 =====
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

@app.errorhandler(Exception)
def _err(e):
    log.exception("Unhandled error on %s", request.path)
    return kakao_text("일시적 오류가 있었지만 연결은 유지되었습니다."), 200

# ===== 헬스/루트/진단 =====
@app.get("/")
def root_ok():
    return Response(b"ok", 200, {"Content-Type": "text/plain"})

@app.get("/healthz")
def healthz():
    return Response(b"ok", 200, {"Content-Type": "text/plain"})

@app.get("/diag")
def diag():
    payload = {
        "api_key_set": bool(API_KEY),
        "chatling_url": CHATLING_URL,
        "body_key": CHATLING_BODY_KEY,
        "timeout_s": CHATLING_TIMEOUT,
        "last_chatling": last_chatling,
        "last_request": last_request,
    }
    if request.args.get("pretty"):
        import json
        return Response(json.dumps(payload, ensure_ascii=False, indent=2),
                        mimetype="application/json")
    return jsonify(payload)

# ===== 유틸 =====
def _is_token(v: Optional[str]) -> bool:
    return isinstance(v, str) and v.strip().startswith("@")

def _resolve_utter(data: Dict[str, Any]) -> str:
    params = (data.get("action") or {}).get("params") or {}
    usrtext = (params.get("usrtext") or "").strip()
    utter_req = ((data.get("userRequest") or {}).get("utterance") or "").strip()
    last_request.update({"raw_usrtext": usrtext, "raw_utterance": utter_req})

    if usrtext and not _is_token(usrtext):
        last_request["source"] = "usrtext"
        return usrtext
    if utter_req and not _is_token(utter_req):
        last_request["source"] = "userRequest.utterance"
        return utter_req
    if utter_req:
        last_request["source"] = "userRequest.utterance(token)"
        return utter_req
    last_request["source"] = "usrtext(token/empty)"
    return usrtext

def _extract_answer(js: Dict[str, Any]) -> Optional[str]:
    # 루트 후보 키
    for k in ("answer", "response", "message", "output", "reply",
              "text", "content", "result"):
        v = js.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # data 내부 후보 키
    data = js.get("data")
    if isinstance(data, dict):
        for k in ("answer", "response", "message", "output", "reply",
                  "text", "content", "result"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    # OpenAI 유사 포맷
    try:
        return js["choices"][0]["message"]["content"].strip()
    except Exception:
        return None

# ===== Chatling 호출 =====
def ask_chatling(utter: str) -> Optional[str]:
    last_chatling.update({
        "ok": False,
        "status": None,
        "body_snippet": None,
        "error": None,
        "url": CHATLING_URL,
    })

    if not utter:
        last_chatling["error"] = "empty_utter"
        return None
    if not API_KEY:
        last_chatling["error"] = "no_api_key"
        return None

    payload: Dict[str, Any] = {CHATLING_BODY_KEY: utter}
    if CHATLING_BOT_ID:
        payload["botId"] = CHATLING_BOT_ID
    if CHATLING_SOURCE_ID:
        payload["sourceId"] = CHATLING_SOURCE_ID

    try:
        r = requests.post(
            CHATLING_URL,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
            timeout=CHATLING_TIMEOUT,
        )
        last_chatling.update({
            "ok": r.ok,
            "status": r.status_code,
            "body_snippet": (r.text[:300] if isinstance(r.text, str) else None),
        })
        if r.ok:
            try:
                js = r.json()
            except Exception:
                return None
            return _extract_answer(js)
    except Exception as e:
        last_chatling["error"] = repr(e)
        log.exception("Chatling call failed")

    return None

# ===== 웹훅 =====
@app.route("/webhook", methods=["POST", "GET", "HEAD"])
def webhook():
    data = request.get_json(silent=True) or {}
    utter = _resolve_utter(data)
    last_request["utter"] = utter

    reply = ask_chatling(utter)
    text = reply or (utter or "연결 OK")  # 폴백: 에코(또는 연결 OK)
    return kakao_text(text), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
