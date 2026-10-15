import hashlib
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

EMAIL = "23f2005605@ds.study.iitm.ac.in".strip().lower()


def solve(challenge: str) -> str:
    text = f"{challenge}:{EMAIL}"
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def handle(msg):
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    # notifications have no id -> no response body needed
    if msg_id is None:
        return None

    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion", "2025-03-26"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "challenge-server", "version": "1.0.0"},
        }
    elif method == "tools/list":
        result = {
            "tools": [{
                "name": "solve_challenge",
                "description": "Solves the exam challenge using request headers.",
                "inputSchema": {"type": "object", "properties": {}},
            }]
        }
    elif method == "tools/call":
        if params.get("name") != "solve_challenge":
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32602, "message": "Unknown tool"}}
        challenge = request.headers.get("X-Exam-Challenge", "").strip()
        result = {"content": [{"type": "text", "text": solve(challenge)}]}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": "Method not found"}}

    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


@app.route("/", methods=["GET"])
def home():
    return "MCP server is running. Use POST /mcp"


@app.route("/mcp", methods=["POST", "GET", "OPTIONS"])
@app.route("/", methods=["POST"])
def mcp():
    if request.method == "OPTIONS":
        return Response(status=204)
    if request.method == "GET":
        return Response(status=405)

    body = request.get_json(silent=True)
    if body is None:
        return jsonify({"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "Parse error"}}), 400

    if isinstance(body, list):
        replies = [r for r in (handle(m) for m in body) if r is not None]
        return (jsonify(replies), 200) if replies else Response(status=202)

    reply = handle(body)
    if reply is None:
        return Response(status=202)
    return jsonify(reply)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)