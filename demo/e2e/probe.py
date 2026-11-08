"""Ask the e2e gateway as a guest and as the owner; the fake model reports what each side could see."""

import json
import os
import urllib.request

BASE = os.environ.get("E2E_HERMES", "http://127.0.0.1:8650")
KEYS = {
    "guest": ("/p/public", "public-e2e-key-fedcba9876543210fedc"),
    "owner": ("", "owner-e2e-key-0123456789abcdef0123"),
}
GUEST_PROMPTS = ["who is he"] + [f"TRY:{tool}" for tool in (
    "read_file", "terminal", "execute_code", "memory", "session_search",
    "skill_view", "vision_analyze", "public_notes_lookup")]


def ask(role: str, prompt: str) -> None:
    prefix, key = KEYS[role]
    body = {"model": "hermes-agent", "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(f"{BASE}{prefix}/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "content-type": "application/json"})
    try:
        out = json.load(urllib.request.urlopen(req, timeout=120))["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        out = f"ERR {exc}"
    print(f"[{role}] {prompt}: {out[:420]}\n")


if __name__ == "__main__":
    for prompt in GUEST_PROMPTS:
        ask("guest", prompt)
    ask("owner", "who am I")
