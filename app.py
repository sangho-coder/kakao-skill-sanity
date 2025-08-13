import os, time, logging
from flask import Flask, request, jsonify, Response, g

logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"),
                    format="%(asctime)s | %(levelname)s | %(message)s")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1*1024*1024

def kakao_text(text:str):
    return jsonify({"version":"2.0","template":{"outputs":[{"simpleText":{"text": text or ""}}]}})

@app.before_request
def _t0(): g.t0 = time.time()

@app.after_request
def _after(r:Response):
    try:
        took=int((time.time()-getattr(g,"t0",time.time()))*1000)
        logging.info("path=%s method=%s status=%s took_ms=%s", request.path, request.method, r.status_code, took)
    except: pass
    return r

@app.errorhandler(Exception)
def _err(e):
    logging.exception("Unhandled error on %s", request.path)
    return kakao_text("일시적 오류가 있었지만 연결은 정상입니다."), 200

# 헬스체크: /healthz 와 / 둘 다 200
@app.get("/healthz")
def healthz(): return Response(b"ok", 200, {"Content-Type":"text/plain"})

@app.get("/")
def root_ok(): return Response(b"ok", 200, {"Content-Type":"text/plain"})

# 웹훅: 항상 카카오 v2.0 JSON 반환
def ask_chatling(utter: str):
    api_key = os.getenv("CHATLING_API_KEY")
    if not (api_key and utter): return None
    try:
        import requests
        r = requests.post(
            os.getenv("CHATLING_URL","https://api.chatling.ai/v1/respond"),
            headers={"Authorization":f"Bearer {api_key}","Content-Type":"application/json","Accept":"application/json"},
            json={"message": utter},
            timeout=float(os.getenv("CHATLING_TIMEOUT","1.8"))
        )
        if r.ok:
            js = r.json()
            return js.get("answer") or js.get("response")
    except Exception:
        logging.exception("chatling call failed")
    return None

@app.route("/webhook", methods=["POST","GET","HEAD"])
def webhook():
    data = request.get_json(silent=True) or {}
    utter = (
        (((data.get("action") or {}).get("params") or {}).get("usrtext")) or
        (((data.get("userRequest") or {}).get("utterance")) or "")
    )
    utter = (utter or "").strip()
    reply = ask_chatling(utter)
    return kakao_text(reply or (utter or "연결 OK")), 200

if __name__=="__main__":
    app.run("0.0.0.0", int(os.getenv("PORT",8080)))
