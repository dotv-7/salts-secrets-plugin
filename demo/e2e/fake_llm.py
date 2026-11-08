"""OpenAI-compatible scripted model for the e2e probe.

A user message ``TRY:<tool>`` makes it call that tool once; any other message gets a REPORT of what
Hermes put in its context: which canaries are present and which tools were offered.
"""

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.environ.get("E2E_ROOT", os.path.expanduser("~/hermes-e2e/root"))
MEMORY = f"{ROOT}/memories/MEMORY.md"
ARGS = {
    "read_file": {"path": MEMORY},
    "terminal": {"command": f"cat {MEMORY}"},
    "memory": {"action": "read", "target": "memory"},
    "session_search": {"query": "secret"},
    "public_notes_lookup": {"query": "Education"},
    "vision_analyze": {"image_url": f"{ROOT}/x.png", "question": "?"},
    "skill_view": {"name": "x"},
    "execute_code": {"code": f"print(open('{MEMORY}').read())"},
}
USAGE = {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}


def text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return " ".join(p.get("text", "") for p in (content or []) if isinstance(p, dict))


def reply(req: dict) -> dict:
    msgs = req.get("messages", [])
    last_user = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=-1)
    question = text(msgs[last_user]) if last_user >= 0 else ""
    tool_results = [text(m) for m in msgs[last_user + 1:] if m.get("role") == "tool"]
    want = question.split("TRY:")[1].split()[0] if "TRY:" in question else None
    if not question.strip():
        return {"role": "assistant", "content": "ok"}
    if want and not tool_results:
        call = {"id": f"call_{int(time.time() * 1000)}", "type": "function",
                "function": {"name": want, "arguments": json.dumps(ARGS.get(want, {}))}}
        return {"role": "assistant", "content": None, "tool_calls": [call]}
    everything = json.dumps(msgs)
    system = "\n".join(text(m) for m in msgs if m.get("role") == "system")
    report = {
        "owner_canary_in_context": "OWNER_SECRET_CANARY" in everything,
        "owner_context_file_in_context": "OWNER_CONTEXT_CANARY" in everything,
        "public_canary_in_context": "PUBLIC_FACT_CANARY" in system,
        "tools_offered": sorted(t["function"]["name"] for t in req.get("tools") or []),
        "tool_result": tool_results[-1][:300] if tool_results else None,
    }
    return {"role": "assistant", "content": "REPORT " + json.dumps(report, ensure_ascii=False)}


def chunk(choices: list, **extra) -> bytes:
    body = {"id": "x", "object": "chat.completion.chunk", "model": "fake", "choices": choices, **extra}
    return f"data: {json.dumps(body)}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        models = {"object": "list", "data": [{"id": "fake", "object": "model", "context_length": 128000}]}
        self.send(200, json.dumps(models).encode())

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        msg = reply(req)
        finish = "tool_calls" if msg.get("tool_calls") else "stop"
        if not req.get("stream"):
            body = {"id": "x", "object": "chat.completion", "model": "fake",
                    "choices": [{"index": 0, "message": msg, "finish_reason": finish}], "usage": USAGE}
            self.send(200, json.dumps(body).encode())
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        delta = {"role": "assistant"}
        if msg.get("tool_calls"):
            delta["tool_calls"] = [dict(msg["tool_calls"][0], index=0)]
        else:
            delta["content"] = msg["content"]
        self.wfile.write(chunk([{"index": 0, "delta": delta, "finish_reason": None}]))
        self.wfile.write(chunk([{"index": 0, "delta": {}, "finish_reason": finish}]))
        self.wfile.write(chunk([], usage=USAGE))
        self.wfile.write(b"data: [DONE]\n\n")


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("E2E_LLM_PORT", "8700"))), Handler).serve_forever()
