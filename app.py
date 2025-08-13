# app.py
import os
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

import requests
from flask import Flask, request, jsonify, Response

# ----------------- Logging -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("kakao-skill")

# ----------------- Env -----------------
CHATLING_API_KEY = os.getenv("CHATLING_API_KEY", "").strip()
CHATLING_URL = os.getenv(
    "CHATLING_URL",
    # 안전장치: 혹시 비워놨다면 기본값을 챗봇ID 자리 표시로 둡니다(배포 전 꼭 교체됨)
    "https://api.chatling.ai/v2/chatbots/9226872959/ai/kb/chat"
).strip()
# v2는 반드시 model id(숫자)가 필요
CHATLING_MODEL_ID_RAW = os.getenv("CHATLING_MODEL_ID", "").strip()
CHATLING_TIMEOUT = float(os.getenv("CHATLING_TIMEOUT", "4.2"))

try:
    CHATLING_MODEL_ID = int(CHATLING_MODEL_ID_RAW) if CHATLING_MODEL_ID_RAW else None
except ValueError:
    CHATLING_MODEL_ID = None

# v2 규격: 본문 키는 'message' 고정 (환경변수로 바꾸지 않음)
V2_BODY_KEY = "message"

# Kakao 표준 응답
def kakao_text(text: str, status: int = 200) -> Response:
    body = {
        "version": "2.0",
        "template": {
            "outputs": [
                {"simpleText": {"text": text}}
            ]
        }
    }
    return Response(json.dumps(body, ensure_ascii=False), status, mimetype="application/json; charset=utf-8")

# ----------------- HTTP session -----------------
_session = requests.Session()
_session.headers.update({
    "Authorization": f"Bearer {CHATLING_API_KEY}" if CHATLING_API_KEY else "",
    "Content-Type": "application/json",
})

# 상태 저장(간단 진단용)
_last_chatling: Dict[str, Any] = {}
_last_request: Dict[str, Any] = {}

app = Flask(__name__)

# ----------------- Health -----------------
@app.get("/")
@app.get("/healthz")
def healthz():
    return Response(b"ok", 200)

# ----------------- Diag -----------------
@app.get("/diag")
def diag():
    pretty = request.args.get("pretty")
    payload = {
        "api_key_set": bool(CHATLING_API_KEY),
        "chatling_url": CHATLING_URL,
        "model_id": CHATLING_MODEL_ID,
        "body_key": V2_BODY_KEY,
        "sync_budget_s": CHATLING_TIMEOUT,
        "last_chatling": _last_chatling or None,
        "last_request": _last_request or None,
    }
    if pretty:
        return Response(json.dumps(payload, ensure_ascii=False, indent=2), 200, mimetype="application/json")
    return jsonify(payload)

# ----------------- Chatling call (v2 고정) -----------------
def call_chatling_v2(message: str) -> Optional[str]:
    global _last_chatling

    if not CHATLING_API_KEY:
        _last_chatling = {"ok": False, "status": 0, "error": "no_api_key"}
        return None
    if not CHATLING_MODEL_ID:
        _last_chatling = {"ok": False, "status": 0, "error": "no_model_id"}
        return None

    payload = {
        V2_BODY_KEY: message,            # <-- 'message'
        "ai_model_id": CHATLING_MODEL_ID # <-- 모델 ID(숫자)
        # 필요 시 옵션 추가 가능: "temperature": 0, "stream": False, ...
    }

    try:
        res = _session.post(CHATLING_URL, json=payload, timeout=CHATLING_TIMEOUT)
        text_snippet = (res.text or "")[:200]
        _last_chatling = {
            "ok": res.ok,
            "status": res.status_code,
            "url": CHATLING_URL,
            "body_snippet": text_snippet
        }
        if not res.ok:
            log.warning("Chatling non-2xx: %s %s", res.status_code, text_snippet)
            return None

        # 응답 추출 (유연하게 처리)
        try:
            j = res.json()
        except Exception:
            # JSON이 아닐 경우 원문 일부 반환
            return text_snippet

        # 흔한 케이스: {"status":"success","data":{"response":"..."}}
        if isinstance(j, dict):
            data = j.get("data") if "data" in j else j
            if isinstance(data, dict):
                for key in ("response", "answer", "text", "message"):
                    if key in data and isinstance(data[key], str):
                        return data[key].strip()

        # 그래도 못 뽑으면 본문 스니펫
        return text_snippet
    except requests.Timeout:
        _last_chatling = {"ok": False, "status": 0, "error": "timeout"}
        return None
    except Exception as e:
        _last_chatling = {"ok": False, "status": 0, "error": str(e)}
        return None

# ----------------- Kakao webhook -----------------
@app.post("/webhook")
def webhook():
    global _last_request

    # 기본 파싱
    data = request.get_json(silent=True) or {}
    utter = (
        (data.get("action", {}).get("params", {}).get("usrtext"))
        or (data.get("userRequest", {}).get("utterance"))
        or ""
    )
    utter = (utter or "").strip()

    _last_request = {
        "utter": utter,
        "source": "action.params.usrtext" if data.get("action", {}).get("params", {}).get("usrtext") else "userRequest.utterance",
        "raw_usrtext": data.get("action", {}).get("params", {}).get("usrtext"),
        "raw_utterance": data.get("userRequest", {}).get("utterance"),
        "ts": datetime.utcnow().isoformat()
    }
    log.info("WEBHOOK utter='%s'", utter)

    if not utter:
        return kakao_text("질문을 입력해 주세요 🙂")

    # v2 호출
    reply = call_chatling_v2(utter)

    if reply:
        return kakao_text(reply)

    # 실패/타임아웃 폴백 (카카오 5초 룰 준수)
    return kakao_text("지금은 답변 서버가 혼잡해요. 잠시 뒤에 다시 시도해 주세요."), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    log.info("running on 0.0.0.0:%s", port)
    app.run(host="0.0.0.0", port=port)
