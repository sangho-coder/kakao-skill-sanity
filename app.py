import os
import time
import json
import logging
import threading
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, request, jsonify, Response, g

# ----------------- 로깅 -----------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("kakao-skill")

# ----------------- 앱 -----------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1_048_576
app.config.update(JSON_AS_ASCII=False)

# ----------------- 환경변수 -----------------
API_KEY = (os.getenv("CHATLING_API_KEY") or "").strip()
CHATLING_URL = (os.getenv("CHATLING_URL") or "https://api.chatling.ai/v1/respond").strip()
# /v2/면 기본 body key를 query로, 아니면 message로
CHATLING_BODY_KEY = os.getenv("CHATLING_BODY_KEY", "query" if "/v2/" in CHATLING_URL else "message")

# 동기(5초 룰) 예산/재시도
SYNC_BUDGET_S = float(os.getenv("SYNC_BUDGET_S", "4.2"))           # 전체 예산
CHATLING_TIMEOUT = float(os.getenv("CHATLING_TIMEOUT", "1.2"))      # 1회 시도 타임아웃
CHATLING_RETRIES_SYNC = int(os.getenv("CHATLING_RETRIES_SYNC", "3"))# 동기 재시도 횟수

# 콜백(백그라운드) 예산/재시도
BG_BUDGET_S = float(os.getenv("CHATLING_BG_BUDGET_S", "20.0"))      # 전체 예산
BG_TRY_TIMEOUT = float(os.getenv("CHATLING_BG_TIMEOUT", "6.0"))     # 1회 시도 타임아웃
BG_SLEEP_BASE = float(os.getenv("CHATLING_BG_SLEEP_BASE", "0.35"))  # 백오프 시작 슬립
WAIT_TEXT = os.getenv("WAIT_TEXT", "답을 찾는 중이에요… 잠시만 기다려 주세요!")

# 선택: 식별자
CHATLING_BOT_ID = os.getenv("CHATLING_BOT_ID")
CHATLING_SOURCE_ID = os.getenv("CHATLING_SOURCE_ID")

# ----------------- HTTP 세션(커넥션 풀 + 네트워크 오류 재시도) -----------------
_session = requests.Session()
_retry = Retry(
    total=0,                 # 상태코드 재시도는 수동으로 함
    connect=2,               # 연결 오류는 2회 정도 내부 재시도
    read=0,
    status=0,
    backoff_factor=0.2,
    allowed_methods=frozenset(["GET", "POST"]),
    raise_on_status=False,
)
_adapter = HTTPAdapter(max_retries=_retry, pool_connections=10, pool_maxsize=50)
_session.mount("http://", _adapter)
_session.mount("https://", _adapter)

# ----------------- 진단용 상태 -----------------
last_chatling: Dict[str, Any] = {
    "ok": False, "status": None, "body_snippet": None, "error": None, "url": CHATLING_URL
}
last_request: Dict[str, Any] = {
    "utter": None, "source": None, "raw_usrtext": None, "raw_utterance": None
}

# ----------------- 유틸 -----------------
def kakao_text(text: str) -> Response:
    return jsonify({"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text or ""}}]}})

def _is_token(v: Optional[str]) -> bool:
    return isinstance(v, str) and v.strip().startswith("@")

def _resolve_utter(data: Dict[str, Any]) -> str:
    params = (data.get("action") or {}).get("params") or {}
    usrtext = (params.get("usrtext") or "").strip()
    utter_req = ((data.get("userRequest") or {}).get("utterance") or "").strip()
    last_request.update({"raw_usrtext": usrtext, "raw_utterance": utter_req})

    if usrtext and not _is_token(usrtext):
        last_request["source"] = "usrtext";    return usrtext
    if utter_req and not _is_token(utter_req):
        last_request["source"] = "userRequest.utterance";  return utter_req
    if utter_req:  # 토큰일 때도 일단 전달
        last_request["source"] = "userRequest.utterance(token)";  return utter_req
    last_request["source"] = "usrtext(token/empty)";  return usrtext

def _extract_answer(js: Dict[str, Any]) -> Optional[str]:
    for k in ("answer","response","message","output","reply","text","content","result"):
        v = js.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    data = js.get("data")
    if isinstance(data, dict):
        for k in ("answer","response","message","output","reply","text","content","result"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    try:
        return js["choices"][0]["message"]["content"].strip()
    except Exception:
        return None

def _payload_for(utter: str) -> Dict[str, Any]:
    p: Dict[str, Any] = {CHATLING_BODY_KEY: utter}
    if CHATLING_BOT_ID: p["botId"] = CHATLING_BOT_ID
    if CHATLING_SOURCE_ID: p["sourceId"] = CHATLING_SOURCE_ID
    return p

def _post_chatling(utter: str, timeout_s: float) -> Optional[str]:
    # 마지막 호출 정보 초기화
    last_chatling.update({"ok": False, "status": None, "body_snippet": None, "error": None, "url": CHATLING_URL})
    if not API_KEY:
        last_chatling["error"] = "no_api_key";   return None
    if not utter:
        last_chatling["error"] = "empty_utter";  return None

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if API_KEY: headers["Authorization"] = f"Bearer {API_KEY}"

    try:
        r = _session.post(CHATLING_URL, headers=headers, json=_payload_for(utter), timeout=timeout_s)
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
        return None
    except Exception as e:
        last_chatling.update({"error": repr(e)})
        log.warning("chatling exception: %s", e)
        return None

def ask_chatling_sync(utter: str) -> Optional[str]:
    """5초 룰 안에서 여러 번 시도 (총 예산 SYNC_BUDGET_S)"""
    deadline = time.time() + SYNC_BUDGET_S
    attempt = 0
    while attempt < CHATLING_RETRIES_SYNC:
        remaining = deadline - time.time()
        if remaining <= 0: break
        timeout = min(max(0.2, CHATLING_TIMEOUT), remaining)
        ans = _post_chatling(utter, timeout)
        if ans: return ans
        attempt += 1
        time.sleep(min(0.15 * attempt, 0.6))  # 짧은 백오프
    return None

def ask_chatling_bg(utter: str) -> str:
    """백그라운드 장시간 재시도 (총 예산 BG_BUDGET_S) — 성공 텍스트 또는 최종 폴백 문구 반환"""
    deadline = time.time() + BG_BUDGET_S
    attempt = 0
    sleep = BG_SLEEP_BASE
    while time.time() < deadline:
        remaining = deadline - time.time()
        timeout = min(BG_TRY_TIMEOUT, max(0.5, remaining))
        ans = _post_chatling(utter, timeout)
        if ans: return ans
        attempt += 1
        time.sleep(min(sleep, 2.0))
        sleep *= 1.6  # 지수 백오프
    return "지금은 답변 서버가 혼잡해요. 잠시 뒤에 다시 시도해 주세요."

def _send_callback(cb_url: str, text: str):
    try:
        body = {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text}}]}}
        r = _session.post(cb_url, json=body, timeout=5)
        log.info("callback status=%s", r.status_code)
    except Exception as e:
        log.warning("callback failed: %s", e)

# ----------------- 미들웨어 로그 -----------------
@app.before_request
def _t0(): g.t0 = time.time()

@app.after_request
def _after(resp: Response):
    try:
        took = int((time.time() - getattr(g, "t0", time.time())) * 1000)
        log.info("path=%s method=%s status=%s took_ms=%s", request.path, request.method, resp.status_code, took)
    except Exception:
        pass
    return resp

@app.errorhandler(Exception)
def _err(e):
    log.exception("Unhandled error")
    return kakao_text("일시적 오류가 있었지만 연결은 유지되었습니다."), 200

# ----------------- 헬스/진단 -----------------
@app.get("/")
def root_ok(): return Response(b"ok", 200, {"Content-Type": "text/plain"})

@app.get("/healthz")
def healthz(): return Response(b"ok", 200, {"Content-Type": "text/plain"})

@app.get("/diag")
def diag():
    payload = {
        "api_key_set": bool(API_KEY),
        "chatling_url": CHATLING_URL,
        "body_key": CHATLING_BODY_KEY,
        "sync_budget_s": SYNC_BUDGET_S,
        "bg_budget_s": BG_BUDGET_S,
        "last_chatling": last_chatling,
        "last_request": last_request,
    }
    if request.args.get("pretty"):
        return Response(json.dumps(payload, ensure_ascii=False, indent=2), mimetype="application/json")
    return jsonify(payload)

# ----------------- 웹훅 -----------------
@app.route("/webhook", methods=["POST", "GET", "HEAD"])
def webhook():
    data = request.get_json(silent=True) or {}
    utter = _resolve_utter(data)
    last_request["utter"] = utter

    # 콜백 모드(있으면): 즉시 응답 후 백그라운드에서 성공할 때까지 재시도
    callback_url = ((data.get("userRequest") or {}).get("callbackUrl"))
    if callback_url:
        def _worker():
            final = ask_chatling_bg(utter)
            _send_callback(callback_url, final)
        threading.Thread(target=_worker, daemon=True).start()
        return jsonify({"version": "2.0", "useCallback": True, "data": {"text": WAIT_TEXT}}), 200

    # 동기 모드: 5초 안에서 여러 번 재시도, 그래도 실패하면 고정 폴백(에코 금지)
    reply = ask_chatling_sync(utter)
    text = reply or "지금은 답변 서버가 느려요. 곧 다시 시도해 볼게요."
    return kakao_text(text), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
