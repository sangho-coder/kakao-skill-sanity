import os, time, json, logging, threading
from datetime import datetime
from typing import Any, Dict, Optional

import requests
from flask import Flask, request, jsonify, Response

# ----------------- Logging -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("kakao-skill")

# ----------------- Env -----------------
API_KEY = os.getenv("CHATLING_API_KEY", "").strip()
CHATLING_URL = os.getenv("CHATLING_URL", "https://api.chatling.ai/v2/chatbots/9226872959/ai/kb/chat").strip()

# 모델 ID는 없어도 OK(빌더 기본 모델 사용). 있으면 정수로 전달.
MODEL_RAW = os.getenv("CHATLING_MODEL_ID", "").strip()
MODEL_ID: Optional[int] = None
try:
    MODEL_ID = int(MODEL_RAW) if MODEL_RAW else None
except Exception:
    MODEL_ID = None

LANGUAGE_ID = int(os.getenv("CHATLING_LANGUAGE_ID", "1"))  # 한국어=1 (선택)
SYNC_TIMEOUT = float(os.getenv("CHATLING_TIMEOUT", "4.6"))  # 동기: 5초 룰 고려
BG_BUDGET_S = float(os.getenv("CHATLING_BG_BUDGET_S", "18"))  # 콜백 백그라운드 총 예산
BG_TRY_TIMEOUT = float(os.getenv("CHATLING_BG_TRY_TIMEOUT", "6.0"))  # 콜백 1회 시도 타임아웃
WAIT_TEXT = os.getenv("WAIT_TEXT", "답을 찾는 중이에요… 잠시만요!")

# ----------------- HTTP -----------------
_session = requests.Session()
_session.headers.update({"Authorization": f"Bearer {API_KEY}" if API_KEY else "", "Content-Type": "application/json", "Accept": "application/json"})

# ----------------- State (diag) -----------------
last_chatling: Optional[Dict[str, Any]] = None
last_request: Optional[Dict[str, Any]] = None

# ----------------- Flask -----------------
app = Flask(__name__)

def kakao_text(text: str, status: int = 200):
    return jsonify({"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text}}]}}), status

def pick_utter(payload: Dict[str, Any]) -> Optional[str]:
    """@text 같은 리터럴 토큰은 버리고 실제 텍스트만 선택"""
    def clean(v):
        if isinstance(v, str):
            v = v.strip()
            if v and v != "@text":
                return v
        return None
    u1 = clean((payload.get("action") or {}).get("params", {}).get("usrtext"))
    u2 = clean((payload.get("userRequest") or {}).get("utterance"))
    return u1 or u2

def extract_answer(js: Any) -> Optional[str]:
    if isinstance(js, dict):
        for k in ("response","message","answer","text","content","reply"):
            v = js.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        data = js.get("data")
        if isinstance(data, dict):
            for k in ("response","message","answer","text","content","reply"):
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        # OpenAI 스타일
        try:
            return js["choices"][0]["message"]["content"].strip()
        except Exception:
            pass
    elif isinstance(js, list):
        for item in js:
            a = extract_answer(item)
            if a: return a
    return None

def call_chatling(message: str, timeout_s: float) -> Optional[str]:
    """v2 호출 (MODEL_ID 없으면 생략, 빌더 기본모델 사용)"""
    global last_chatling
    if not API_KEY or not CHATLING_URL:
        last_chatling = {"ok": False, "status": 0, "error": "missing_api_key_or_url"}
        return None

    payload: Dict[str, Any] = {"message": message, "language_id": LANGUAGE_ID}
    if MODEL_ID is not None:
        payload["ai_model_id"] = MODEL_ID

    t0 = time.time()
    try:
        r = _session.post(CHATLING_URL, data=json.dumps(payload), timeout=timeout_s)
        took = int((time.time() - t0) * 1000)
        snippet = (r.text or "")[:400]
        last_chatling = {"ok": r.ok, "status": r.status_code, "took_ms": took, "url": CHATLING_URL, "body_snippet": snippet}
        log.info("chatling status=%s took_ms=%s", r.status_code, took)
        if not r.ok:
            return None
        try:
            js = r.json()
        except Exception:
            return snippet or None
        return extract_answer(js) or snippet or None
    except requests.Timeout:
        last_chatling = {"ok": False, "status": 0, "error": "timeout"}
        return None
    except Exception as e:
        last_chatling = {"ok": False, "status": 0, "error": repr(e)}
        log.exception("chatling call failed")
        return None

def send_callback(cb_url: str, text: str):
    try:
        body = {"version":"2.0","template":{"outputs":[{"simpleText":{"text":text}}]}}
        r = _session.post(cb_url, json=body, timeout=5)
        log.info("callback status=%s", r.status_code)
    except Exception as e:
        log.warning("callback failed: %s", e)

def bg_worker(callback_url: str, utter: str):
    """콜백 모드: 예산 내에서 반복 시도"""
    deadline = time.time() + BG_BUDGET_S
    sleep = 0.35
    result = None
    while time.time() < deadline:
        result = call_chatling(utter, timeout_s=min(BG_TRY_TIMEOUT, deadline - time.time()))
        if result:
            break
        time.sleep(min(sleep, 2.0))
        sleep *= 1.6
    send_callback(callback_url, result or "지금은 답변 서버가 혼잡해요. 잠시 뒤에 다시 시도해 주세요.")

# ----------------- Health & Diag & Probe -----------------
@app.get("/")
@app.get("/healthz")
def health():
    return Response(b"ok", 200)

@app.get("/diag")
def diag():
    pretty = request.args.get("pretty")
    body = {
        "api_key_set": bool(API_KEY),
        "chatling_url": CHATLING_URL,
        "model_id": MODEL_ID,
        "body_key": "message",
        "sync_budget_s": SYNC_TIMEOUT,
        "bg_budget_s": BG_BUDGET_S,
        "last_chatling": last_chatling,
        "last_request": last_request,
    }
    if pretty:
        return Response(json.dumps(body, ensure_ascii=False, indent=2), 200, {"Content-Type": "application/json"})
    return jsonify(body)

@app.get("/probe")
def probe():
    """브라우저에서 바로 Chatling 연결 점검: /probe?q=안녕"""
    q = request.args.get("q", "안녕하세요! 연결 점검입니다.")
    ans = call_chatling(q, timeout_s=4.0)
    # ans가 있어도 일부만 노출
    preview = (ans[:180] + "…") if isinstance(ans, str) and len(ans) > 180 else ans
    return jsonify({"sent": q, "ok": bool(ans), "answer_preview": preview, "last_chatling": last_chatling}), 200

# ----------------- Webhook -----------------
@app.post("/webhook")
def webhook():
    global last_request
    data = request.get_json(silent=True) or {}
    utter = pick_utter(data)
    last_request = {
        "utter": utter if utter else "(none)",
        "raw_usrtext": (data.get("action") or {}).get("params", {}).get("usrtext"),
        "raw_utterance": (data.get("userRequest") or {}).get("utterance"),
        "ts": datetime.utcnow().isoformat(),
    }

    if not utter:
        return kakao_text("무슨 말씀인지 조금만 더 자세히 알려주세요 🙂")

    # 콜백 모드: 카카오가 callbackUrl을 줄 경우
    callback_url = (data.get("userRequest") or {}).get("callbackUrl")
    if callback_url:
        threading.Thread(target=bg_worker, args=(callback_url, utter), daemon=True).start()
        return jsonify({"version": "2.0", "useCallback": True, "data": {"text": WAIT_TEXT}}), 200

    # 동기 모드: 5초 안에서 바로 시도
    ans = call_chatling(utter, timeout_s=SYNC_TIMEOUT)
    return kakao_text(ans or "지금은 답변 서버가 혼잡해요. 잠시 뒤에 다시 시도해 주세요.")

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
