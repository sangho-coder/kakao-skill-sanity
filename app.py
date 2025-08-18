import os, time, json, logging, threading, re, html
from datetime import datetime
from typing import Any, Dict, Optional

import requests
from flask import Flask, request, jsonify, Response

# ----------------- Logging -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("kakao-skill")

# ----------------- Env (Chatling) -----------------
API_KEY = os.getenv("CHATLING_API_KEY", "").strip()
# 인천문화재단 챗봇 기본값(필요 시 환경변수 CHATLING_URL로 덮어쓰기)
CHATLING_URL = os.getenv(
    "CHATLING_URL",
    "https://api.chatling.ai/v2/chatbots/9226872959/ai/kb/chat"
).strip()

MODEL_RAW = os.getenv("CHATLING_MODEL_ID", "").strip()  # 선택(없으면 빌더 기본 모델)
MODEL_ID: Optional[int] = None
try:
    MODEL_ID = int(MODEL_RAW) if MODEL_RAW else None
except Exception:
    MODEL_ID = None

LANGUAGE_ID = int(os.getenv("CHATLING_LANGUAGE_ID", "1"))  # 한국어=1

# --- 타임아웃 정책 ---
# 동기 응답(5초 제한 고려): 바로 대기안내만 시도 → 2s
SYNC_TIMEOUT = float(os.getenv("CHATLING_TIMEOUT", "2.0"))
# 콜백 총 예산(카카오 콜백 유효시간 60s보다 항상 작게): 50s
BG_BUDGET_S = float(os.getenv("CHATLING_BG_BUDGET_S", "50"))
# 콜백 1회 시도 read timeout: 40s
BG_TRY_TIMEOUT = float(os.getenv("CHATLING_BG_TRY_TIMEOUT", "40.0"))

WAIT_TEXT = os.getenv("WAIT_TEXT", "답을 찾는 중이에요… 잠시만요!")

# ----------------- Env (Rate Limit & Ban) -----------------
RL_PER_MIN   = int(os.getenv("RL_PER_MIN", "10"))
RL_PER_HOUR  = int(os.getenv("RL_PER_HOUR", "200"))
RL_PER_DAY   = int(os.getenv("RL_PER_DAY", "1000"))
RL_COOLDOWN_SHORT = int(os.getenv("RL_COOLDOWN_SHORT", "600"))
RL_BAN_DAYS       = int(os.getenv("RL_BAN_DAYS", "30"))
RL_STRIKE_WINDOW_DAYS = int(os.getenv("RL_STRIKE_WINDOW_DAYS", "7"))
SPAM_BURST_N = int(os.getenv("SPAM_BURST_N", "10"))
SPAM_BURST_S = float(os.getenv("SPAM_BURST_S", "2.0"))

# 선택: Redis (분산 환경 권장)
REDIS_URL = os.getenv("REDIS_URL", "").strip()
_redis = None
if REDIS_URL:
    try:
        import redis  # type: ignore
        _redis = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=1.0)
        _redis.ping()
        log.info("Redis connected")
    except Exception as e:
        log.warning("Redis unavailable, falling back to in-memory: %s", e)
        _redis = None

# ----------------- HTTP -----------------
# Chatling 호출용 세션(Authorization 포함)
_session = requests.Session()
_session.headers.update({
    "Authorization": f"Bearer {API_KEY}" if API_KEY else "",
    "Content-Type": "application/json",
    "Accept": "application/json"
})

# ----------------- State (diag) -----------------
last_chatling: Optional[Dict[str, Any]] = None
last_request: Optional[Dict[str, Any]] = None

# ----------------- Helpers -----------------
def to_plain_text(s: Optional[str]) -> Optional[str]:
    """Markdown/HTML 제거 → Plain Text. 링크는 '텍스트 (URL)' 형태로 유지."""
    if not s:
        return s
    s = html.unescape(s)

    # 코드펜스/인라인코드 제거
    s = re.sub(r"```[ \t]*[a-zA-Z0-9_+\-]*\n?", "", s)
    s = s.replace("```", "")
    s = re.sub(r"`([^`]+)`", r"\1", s)

    # 볼드/이탤릭/취소선 제거
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"\*([^*]+)\*", r"\1", s)
    s = re.sub(r"__([^_]+)__", r"\1", s)
    s = re.sub(r"_([^_]+)_", r"\1", s)
    s = re.sub(r"~~([^~]+)~~", r"\1", s)

    # 헤딩 기호 제거
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.M)

    # 링크/이미지
    s = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\1 (\2)", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", s)

    # HTML 태그 제거
    s = re.sub(r"<[^>]+>", "", s)

    # 리스트 마커 통일
    s = re.sub(r"^\s*[-*+]\s+", "- ", s, flags=re.M)
    s = re.sub(r"^[•▪︎●]\s*", "- ", s, flags=re.M)

    # nbsp 등 공백 통일
    s = s.replace("\u00A0", " ")
    return s.strip()

# ----------------- Flask -----------------
app = Flask(__name__)

def kakao_text(text: str, status: int = 200):
    text = to_plain_text(text) or ""
    return jsonify({"version": "2.0",
                    "template": {"outputs": [{"simpleText": {"text": text}}]}}), status

def _get(d: Dict[str, Any], *path) -> Optional[Any]:
    cur = d
    for k in path:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur

def pick_utter(payload: Dict[str, Any]) -> Optional[str]:
    """@text 같은 리터럴 토큰 제거 후 실제 텍스트 선택"""
    def clean(v):
        if isinstance(v, str):
            v = v.strip()
            if v and v != "@text":
                return v
        return None
    u1 = clean(_get(payload, "action", "params", "usrtext"))
    u2 = clean(_get(payload, "userRequest", "utterance"))
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
        try:
            return js["choices"][0]["message"]["content"].strip()  # OpenAI 스타일
        except Exception:
            pass
    elif isinstance(js, list):
        for item in js:
            a = extract_answer(item)
            if a:
                return a
    return None

def call_chatling(message: str, timeout_s: float) -> Optional[str]:
    """Chatling v2 호출 (MODEL_ID 없으면 빌더 기본 모델 사용)"""
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
        last_chatling = {"ok": r.ok, "status": r.status_code, "took_ms": took,
                         "url": CHATLING_URL, "body_snippet": snippet}
        log.info("chatling status=%s took_ms=%s", r.status_code, took)
        if not r.ok:
            return None
        try:
            js = r.json()
            text = extract_answer(js) or snippet or None
        except Exception:
            text = snippet or None
        return to_plain_text(text) if text else None
    except requests.Timeout:
        last_chatling = {"ok": False, "status": 0, "error": "timeout"}
        return None
    except Exception as e:
        last_chatling = {"ok": False, "status": 0, "error": repr(e)}
        log.exception("chatling call failed")
        return None

def send_callback(cb_url: str, text: str, cb_token: Optional[str]):
    """카카오 콜백: 반드시 x-kakao-callback-token 포함. Chatling 세션 헤더와 분리."""
    try:
        text = to_plain_text(text) or ""
        body = {"version":"2.0","template":{"outputs":[{"simpleText":{"text":text}}]}}
        headers = {"Content-Type":"application/json"}
        if cb_token:
            headers["x-kakao-callback-token"] = cb_token
        r = requests.post(cb_url, json=body, headers=headers, timeout=(2, 10))
        log.info("callback status=%s", r.status_code)
    except Exception as e:
        log.warning("callback failed: %s", e)

def bg_worker(callback_url: str, cb_token: Optional[str], utter: str):
    """콜백 모드: 예산 내에서 반복 시도 → 50s 이내 1회 콜백"""
    deadline = time.time() + min(BG_BUDGET_S, 50)
    sleep = 0.35
    result = None
    while time.time() < deadline:
        result = call_chatling(utter, timeout_s=min(BG_TRY_TIMEOUT, max(0.1, deadline - time.time())))
        if result:
            break
        time.sleep(min(sleep, 2.0))
        sleep *= 1.6
    send_callback(callback_url, result or "지금은 답변 서버가 혼잡해요. 잠시 뒤에 다시 시도해 주세요.", cb_token)

# ----------------- Rate Limit / Ban (per-user) -----------------
from collections import defaultdict
_mem_buckets = defaultdict(lambda: {
    "ban_exp": 0,
    "burst": [],
    "m": {"count":0, "exp":0},
    "h": {"count":0, "exp":0},
    "d": {"count":0, "exp":0},
    "strikes": 0,
    "strike_exp": 0,
})

def _fmt_remain_ko(seconds: int) -> str:
    if seconds <= 0: return "잠시 후"
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d: return f"{d}일 {h}시간"
    if h: return f"{h}시간 {m}분"
    if m: return f"{m}분 {s}초"
    return f"{s}초"

def _incr_mem_bucket(bkt: dict, window_s: int) -> int:
    now = time.time()
    if now > bkt["exp"]:
        bkt["count"] = 0
        bkt["exp"] = now + window_s
    bkt["count"] += 1
    return bkt["count"]

def _redis_incr_with_ttl(key: str, window_s: int) -> int:
    assert _redis is not None
    pipe = _redis.pipeline()
    pipe.incr(key, 1)
    pipe.ttl(key)
    cnt, ttl = pipe.execute()
    if ttl in (-1, -2):
        _redis.expire(key, window_s)
    return int(cnt)

def _redis_ttl(key: str) -> int:
    try:
        t = _redis.ttl(key)
        return int(t) if t and t > 0 else 0
    except Exception:
        return 0

def _redis_setex(key: str, seconds: int, value: str = "1") -> None:
    _redis.setex(key, seconds, value)

def _redis_get_int(key: str, default: int = 0) -> int:
    try:
        v = _redis.get(key)
        return int(v) if v is not None else default
    except Exception:
        return default

def rate_limit_check_and_message(user_id: str):
    now = int(time.time())

    # 0) 기존 차단 여부 확인
    if _redis:
        ban_key = f"ban:{user_id}"
        ban_ttl = _redis_ttl(ban_key)
        if ban_ttl > 0:
            msg = f"이용이 제한되어 있어요. { _fmt_remain_ko(ban_ttl) } 후 다시 시도해 주세요."
            if ban_ttl >= (RL_BAN_DAYS-1) * 86400:
                msg = ("반복적인 과도 이용으로 이용이 제한되어 있어요. "
                       f"{ _fmt_remain_ko(ban_ttl) } 후 다시 이용해 주세요.")
            return False, msg
    else:
        u = _mem_buckets[user_id]
        if now < u["ban_exp"]:
            remain = u["ban_exp"] - now
            msg = f"이용이 제한되어 있어요. { _fmt_remain_ko(remain) } 후 다시 시도해 주세요."
            if remain >= (RL_BAN_DAYS-1)*86400:
                msg = ("반복적인 과도 이용으로 이용이 제한되어 있어요. "
                       f"{ _fmt_remain_ko(remain) } 후 다시 이용해 주세요.")
            return False, msg

    # 1) 버스트 탐지
    if _redis:
        burst_key = f"burst:{user_id}"
        burst_cnt = _redis_incr_with_ttl(burst_key, int(SPAM_BURST_S))
        if burst_cnt > SPAM_BURST_N:
            strikes_key = f"str:{user_id}"
            strikes = _redis_get_int(strikes_key, 0) + 1
            _redis_setex(strikes_key, RL_STRIKE_WINDOW_DAYS*86400, str(strikes))
            if strikes >= 2:
                _redis_setex(f"ban:{user_id}", RL_BAN_DAYS*86400, "1")
                return False, "반복적인 과도 이용으로 30일 동안 이용이 제한되었어요. 문의가 필요하면 고객센터로 연락해 주세요."
            else:
                _redis_setex(f"ban:{user_id}", RL_COOLDOWN_SHORT, "1")
                return False, (f"요청이 너무 빠르게 이어지고 있어요. "
                               f"{_fmt_remain_ko(RL_COOLDOWN_SHORT)} 후 다시 이용해 주세요. "
                               "같은 현상이 반복되면 최대 30일 동안 이용이 제한될 수 있어요.")
    else:
        u = _mem_buckets[user_id]
        t_now = time.time()
        u["burst"] = [t for t in u["burst"] if t_now - t <= SPAM_BURST_S]
        u["burst"].append(t_now)
        if len(u["burst"]) > SPAM_BURST_N:
            if u["strike_exp"] < now:
                u["strikes"] = 0
                u["strike_exp"] = now + RL_STRIKE_WINDOW_DAYS*86400
            u["strikes"] += 1
            if u["strikes"] >= 2:
                u["ban_exp"] = now + RL_BAN_DAYS*86400
                return False, "반복적인 과도 이용으로 30일 동안 이용이 제한되었어요. 문의가 필요하면 고객센터로 연락해 주세요."
            else:
                u["ban_exp"] = now + RL_COOLDOWN_SHORT
                return False, (f"요청이 너무 빠르게 이어지고 있어요. "
                               f"{_fmt_remain_ko(RL_COOLDOWN_SHORT)} 후 다시 이용해 주세요. "
                               "같은 현상이 반복되면 최대 30일 동안 이용이 제한될 수 있어요.")

    # 2) 분/시간/일 한도
    if _redis:
        if _redis_incr_with_ttl(f"m:{user_id}", 60) > RL_PER_MIN:
            strikes_key = f"str:{user_id}"
            strikes = _redis_get_int(strikes_key, 0) + 1
            _redis_setex(strikes_key, RL_STRIKE_WINDOW_DAYS*86400, str(strikes))
            if strikes >= 2:
                _redis_setex(f"ban:{user_id}", RL_BAN_DAYS*86400, "1")
                return False, "반복적인 과도 이용으로 30일 동안 이용이 제한되었어요. 문의가 필요하면 고객센터로 연락해 주세요."
            else:
                _redis_setex(f"ban:{user_id}", RL_COOLDOWN_SHORT, "1")
                return False, (f"요청이 많아 이용이 잠시 제한되었어요. "
                               f"{_fmt_remain_ko(RL_COOLDOWN_SHORT)} 후 다시 이용해 주세요. "
                               "같은 현상이 반복되면 최대 30일 동안 이용이 제한될 수 있어요.")
        if _redis_incr_with_ttl(f"h:{user_id}", 3600) > RL_PER_HOUR:
            _redis_setex(f"ban:{user_id}", RL_COOLDOWN_SHORT, "1")
            return False, (f"시간당 이용 한도에 도달했어요. "
                           f"{_fmt_remain_ko(RL_COOLDOWN_SHORT)} 후 다시 이용해 주세요. "
                           "같은 현상이 반복되면 최대 30일 동안 이용이 제한될 수 있어요.")
        if _redis_incr_with_ttl(f"d:{user_id}", 86400) > RL_PER_DAY:
            _redis_setex(f"ban:{user_id}", RL_BAN_DAYS*86400, "1")
            return False, "일일 이용 한도를 초과했어요. 30일 동안 이용이 제한될 수 있어요."
    else:
        u = _mem_buckets[user_id]
        if _incr_mem_bucket(u["m"], 60) > RL_PER_MIN:
            if u["strike_exp"] < now:
                u["strikes"] = 0
                u["strike_exp"] = now + RL_STRIKE_WINDOW_DAYS*86400
            u["strikes"] += 1
            if u["strikes"] >= 2:
                u["ban_exp"] = now + RL_BAN_DAYS*86400
                return False, "반복적인 과도 이용으로 30일 동안 이용이 제한되었어요. 문의가 필요하면 고객센터로 연락해 주세요."
            else:
                u["ban_exp"] = now + RL_COOLDOWN_SHORT
                return False, (f"요청이 많아 이용이 잠시 제한되었어요. "
                               f"{_fmt_remain_ko(RL_COOLDOWN_SHORT)} 후 다시 이용해 주세요. "
                               "같은 현상이 반복되면 최대 30일 동안 이용이 제한될 수 있어요.")
        if _incr_mem_bucket(u["h"], 3600) > RL_PER_HOUR:
            u["ban_exp"] = now + RL_COOLDOWN_SHORT
            return False, (f"시간당 이용 한도에 도달했어요. "
                           f"{_fmt_remain_ko(RL_COOLDOWN_SHORT)} 후 다시 이용해 주세요. "
                           "같은 현상이 반복되면 최대 30일 동안 이용이 제한될 수 있어요.")
        if _incr_mem_bucket(u["d"], 86400) > RL_PER_DAY:
            u["ban_exp"] = now + RL_BAN_DAYS*86400
            return False, "일일 이용 한도를 초과했어요. 30일 동안 이용이 제한될 수 있어요."

    return True, None

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
        return Response(json.dumps(body, ensure_ascii=False, indent=2),
                        200, {"Content-Type": "application/json"})
    return jsonify(body)

@app.get("/probe")
def probe():
    q = request.args.get("q", "안녕하세요! 연결 점검입니다.")
    ans = call_chatling(q, timeout_s=4.0)
    preview = (ans[:180] + "…") if isinstance(ans, str) and len(ans) > 180 else ans
    return jsonify({"sent": q, "ok": bool(ans), "answer_preview": preview, "last_chatling": last_chatling}), 200

# ----------------- Webhook -----------------
@app.post("/webhook")
def webhook():
    global last_request
    data = request.get_json(silent=True) or {}

    # 사용자 식별자
    user_id = _get(data, "userRequest", "user", "id") or "anon"

    # 레이트리밋/차단
    allowed, msg = rate_limit_check_and_message(user_id)
    if not allowed:
        return kakao_text(msg)

    utter = pick_utter(data)
    last_request = {
        "user_id": user_id,
        "utter": utter if utter else "(none)",
        "raw_usrtext": _get(data, "action", "params", "usrtext"),
        "raw_utterance": _get(data, "userRequest", "utterance"),
        "has_callback": bool(_get(data, "userRequest", "callbackUrl")),
        "ts": datetime.utcnow().isoformat(),
    }

    if not utter:
        return kakao_text("무슨 말씀인지 조금만 더 자세히 알려주세요 🙂")

    # 콜백 모드
    callback_url = _get(data, "userRequest", "callbackUrl")
    cb_token = request.headers.get("x-kakao-callback-token")
    if callback_url:
        log.info("callback mode: useCallback=True → final will be sent via callback within 50s")
        threading.Thread(target=bg_worker, args=(callback_url, cb_token, utter), daemon=True).start()
        return jsonify({"version": "2.0", "useCallback": True, "data": {"text": WAIT_TEXT}}), 200

    # 동기 모드
    ans = call_chatling(utter, timeout_s=SYNC_TIMEOUT)
    return kakao_text(ans or "지금은 답변 서버가 혼잡해요. 잠시 뒤에 다시 시도해 주세요.")

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
