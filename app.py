import os
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.get("/healthz")
def healthz():
    return ("ok", 200, {"Content-Type": "text/plain"})

@app.route("/webhook", methods=["POST", "GET", "HEAD"])
def webhook():
    return jsonify({
        "version": "2.0",
        "template": { "outputs": [ { "simpleText": { "text": "연결 OK" } } ] }
    }), 200

if __name__ == "__main__":
    app.run("0.0.0.0", int(os.getenv("PORT", 8080)))
