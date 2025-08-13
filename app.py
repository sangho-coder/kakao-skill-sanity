import os
from flask import Flask, Response, request, jsonify

app = Flask(__name__)

@app.get("/healthz")
def healthz():
    return Response(b"ok", 200, mimetype="text/plain")

@app.route("/webhook", methods=["POST","GET","HEAD"])
def webhook():
    # 카카오 연결 확인용: 요청을 보지 않아도 항상 200 JSON
    return jsonify({
        "version": "2.0",
        "template": {"outputs":[{"simpleText":{"text":"연결 OK"}}]}
    }), 200

if __name__ == "__main__":
    app.run("0.0.0.0", int(os.getenv("PORT", 8080)))
