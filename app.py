import os, time, json, logging
from datetime import datetime
from typing import Any, Dict, Optional

import requests
from flask import Flask, request, jsonify, Response

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("kakao-skill")

# ---------- Flask ----------
app = Flask(__name__)

# ---------- Env ----------
CHATLING_API_KEY   = os.getenv("CHATLING_API_KEY", "").strip()
CHATLING_URL       = os.getenv("CHATLING_URL", "").strip()  # e.g. https://api.chatling.ai/v2/chatbots/9226872959/ai/kb/chat
CHATLING_MODEL_ID  = os.getenv("CHATLING_MODEL_ID", "").strip()  # optional
CHATLING_TIMEOUT_S = float(os.getenv("CHATLING_TIMEOUT", "4.2"))

_session = requests.Session()
_last_chatling: Optional[Dict[str, Any]] = None
_last_request:  Optional[Dict[str, Any]] = None

# ---------- Helpers ----------
def kakao_text(text: str, status: int = 200):
    """Kakao v2 simpleText response"""
    return jsonify({
        "version": "2.0",
        "template": {"outputs": [{"simpleText": {"text": text}}]}
    }), status

def _pick_utterance(payload: Dict[str, Any]) -> Optional[str]:
    """Pick user message, ignoring literal '@text' placeholders."""
    u1 = (payload.get("userRequest") or {}).get("utterance")
    u2 = ((payload.get("action") or {}).get("params") or {}).get("usrtext")

    # Normalize and ignore literal '@text'
    def clean(v):
        if isinstance(v, str):
            v = v.strip()
            if v and v != "@text":
                return v
        return None

    for cand in (clean(u1), clean(u2)):
        if cand:
            return cand
    return None

def _extract_answer(j: Any) -> Optional[str]:
    """Best-effort text extraction from Chatling response JSON."""
    if isinstance(j, dict):
        # Common keys first
        for k in ("message", "answer", "content", "text", "reply"):
            v = j.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        # nested data
        if "data" in j:
            return _extract_answer(j["data"])
        if "choices" in j and isinstance(j["choices"], list):
            for c in j["choices"]:
                a = _extract_answer(c)
                if a:
                    return a
    elif isinstance(j, list):
        for item in j:
            a = _extract_answer(item)
            if a:
                return a
    return None

# ---------- Routes ----------
@app.get("/")
def root():
    return Response(b"OK", 200)

@app.get("/healthz")
def healthz():
    if not CHATLING_API_KEY or not CHATLING_URL:
        return Response(b"misconfigured", 500)
    return Response(b"ok", 200)

@app.get("/diag")
def diag():
    pretty = request.args.get("pretty")
    body: Dict[str, Any] = {
        "api_key_set": bool(CHATLING_API_KEY),
        "chatling_url": CHATLING_URL,
        "model_id": CHATLING_MODEL_ID or None,
        "body_key": "message",
        "sync_budget_s": CHATLING_TIMEOUT_S,
        "last_chatling": _last_chatling,
        "last_request": _last_request,
    }
    if pretty:
        return Response(json.dumps(body, ensure_ascii=False, indent=2), 200, {"Content-Type": "application/json"})
    return jsonify(body)

@app.post("/webhook")
def webhook():
    global _last_chatling, _last_request

    ts = datetime.utcnow().isoformat()
    data = request.get_json(silent=True) or {}
    utter = _pick_utterance(data)

    _last_request = {
        "utter": utter if utter is not None else "(none)",
        "source": "userRequest.utterance > action.params.usrtext",
        "raw_usrtext": ((data.get("action") or {}).get("params") or {}).get("usrtext"),
        "raw_utterance": (data.get("userRequest") or {}).get("utterance"),
        "ts": ts,
    }

    if not utter:
        return kakao_text("무슨 말씀인지 조금만 더 자세히 알려주세요 🙂")

    if not CHATLING_API_KEY or not CHATLING_URL:
        log.error("Missing CHATLING_API_KEY or CHATLING_URL")
        return kakao_text("지금은 답변 서버가 혼잡해요. 잠시 뒤에 다시 시도해 주세요.")

    headers = {
        "Authorization": f"Bearer {CHATLING_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    payload: Dict[str, Any] = {"message": utter}
    # Only include ai_model_id when provided
    if CHATLING_MODEL_ID:
        try:
            payload["ai_model_id"] = int(CHATLING_MODEL_ID)
        except Exception:
            log.warning("CHATLING_MODEL_ID is not an integer; ignoring.")

    t0 = time.time()
    try:
        r = _session.post(
            CHATLING_URL,
            headers=headers,
            json=payload,
            timeout=CHATLING_TIMEOUT_S
        )
        took_ms = int((time.time() - t0) * 1000)

        # Save diagnostic
        snippet = r.text[:400] if r.text else ""
        _last_chatling = {
            "ok": r.ok,
            "status": r.status_code,
            "url": CHATLING_URL,
            "took_ms": took_ms,
            "body_snippet": snippet,
        }
        log.info("Chatling status=%s took_ms=%s", r.status_code, took_ms)

        if not r.ok:
            # Typical helpful messages bubble up to client logs via diag
            return kakao_text("지금은 답변 서버가 혼잡해요. 잠시 뒤에 다시 시도해 주세요.")

        # Parse and pick best text
        try:
            j = r.json()
        except Exception:
            j = None
        answer = _extract_answer(j) if j is not None else None
        if not answer:
            # Fallback to raw text (truncated) if no clear field
            answer = snippet if snippet else "죄송해요, 잠시 후 다시 시도해 주세요."

        return kakao_text(answer)

    except requests.Timeout:
        _last_chatling = {"ok": False, "status": 0, "error": "timeout"}
        return kakao_text("지금은 답변 서버가 혼잡해요. 잠시 뒤에 다시 시도해 주세요.")
    except Exception as e:
        _last_chatling = {"ok": False, "status": 0, "error": str(e)}
        log.exception("Chatling call failed")
        return kakao_text("지금은 답변 서버가 혼잡해요. 잠시 뒤에 다시 시도해 주세요.")

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
