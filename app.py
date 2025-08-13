# 상단 import 근처에 추가
import os, time, logging, requests
from flask import Flask, request, jsonify, Response, g

API_KEY = os.getenv("CHATLING_API_KEY")
CHATLING_URL = os.getenv("CHATLING_URL", "https://api.chatling.ai/v1/respond")
TIMEOUT = float(os.getenv("CHATLING_TIMEOUT", "1.8"))
last_chatling = {"ok": False, "status": None, "body_snippet": None, "error": None}

def ask_chatling(utter: str):
    # 기록 초기화
    last_chatling.update({"ok": False, "status": None, "body_snippet": None, "error": None})
    if not (API_KEY and utter):
        if not API_KEY:
            last_chatling["error"] = "no_api_key"
        elif not utter:
            last_chatling["error"] = "empty_utter"
        return None
    try:
        r = requests.post(
            CHATLING_URL,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            # ⚠️ Chatling의 실제 스키마에 맞게 필요시 body 수정
            json={"message": utter},
            timeout=TIMEOUT,
        )
        last_chatling.update({"ok": r.ok, "status": r.status_code, "body_snippet": r.text[:200]})
        if r.ok:
            js = r.json()
            # ⚠️ 실제 필드명에 맞게 조정 (예: js["answer"] / js["response"] / js["data"]["output"] ...)
            return js.get("answer") or js.get("response")
    except Exception as e:
        last_chatling["error"] = repr(e)
    return None

# 최근 상태를 확인하는 엔드포인트 (키는 노출 X)
@app.get("/diag")
def diag():
    return jsonify({
        "api_key_set": bool(API_KEY),
        "chatling_url": CHATLING_URL,
        "timeout_s": TIMEOUT,
        "last_chatling": last_chatling,
    }), 200
