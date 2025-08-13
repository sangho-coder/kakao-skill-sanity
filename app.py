import os, time, logging
from datetime import datetime
from flask import Flask, request, jsonify, Response, g
import requests

# ----- 로깅 -----
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("kakao-skill")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # 1MB

API_KEY = os.getenv("CHATLING_API_KEY")  # 없으면 None

# ----- 공용 응답 -----
def kakao_text(text: str):
    return jsonify({
        "version": "2.0",
        "template": {
            "outputs": [{"simpleText": {"text": text or ""}}]
        }
    })

# ----- 액세스 로그 -----
@app.before_request
def _t0(): g.t0 = time.time()

@app.after_request
def _after(resp: Response):
    try:
        took = int((time.time() - getattr(g, "t0", time.time())) * 1000)
        log.info("path=%s method=%s status=%s took_ms=%s", request.path, request.method, resp.status_code, took)
    except:  # noqa: E722
        pass
    return resp

# ----- 전역 예외: 항상 200 JSON (제로-페일) -----
@app.errorhandler(Exception)
def _err(e):
    log.exception("Unhandled error on %s", request.path)
    return kakao_text("일시적 오류가 있었지만 연결은 정상입니다."), 200

# ----- 헬스체크 -----
@app.get("/healthz")
def healthz():
    return Response(b"ok", 200, mimetype="text/plain")

# ----- Chatling 호출(키 없거나 실패하면 None) -----
def ask_chatling(utter: str):
    if not (API_KEY and utter):  # 키 없거나 입력 없음
        return None
    try:
        r = requests.post(
            "https://api.chatling.ai/v1/respond",      # 필요 시 실제 스펙에 맞춰 필드 수정
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={"message": utter},
            timeout=1.8  # 카카오 타임아웃 내 즉시 포기
        )
        if r.ok:
            js = r.json()
            return js.get("answer") or js.get("response") or None
    except Exception:
        log.exception("chatling call failed")
    return None

# ----- 카카오 스킬 웹훅 -----
@app.route("/webhook", methods=["POST", "GET", "HEAD"])
def webhook():
    # 어떤 메서드로 와도 JSON 반환 (검증/테스트 보호)
    data = request.get_json(silent=True) or {}
    utter = (
        (((data.get("action") or {}).get("params") or {}).get("usrtext")) or
        (((data.get("userRequest") or {}).get("utterance")) or "")
    )
    utter = (utter or "").strip()

    reply = ask_chatling(utter)
    text = reply or (utter or "연결 OK")  # 실패/키없음 → 즉시 대체응답
    return kakao_text(text), 200

# 로컬 실행용
if __name__ == "__main__":
    app.run("0.0.0.0", int(os.getenv("PORT", 8080)))
