"""Web chat in front of Hermes, with owner-only Context, Permissions and Trace pages.

The browser never sees a Hermes API key. Each tier talks to its own profile: guests to
``/p/<public>/v1``, invited members to ``/p/<tier profile>/v1``, the owner (passphrase) to the
default profile. Blocked visitors never reach Hermes.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from salts_secrets import _yaml as yaml
from salts_secrets import board, classify, guests
from salts_secrets import context as ctx
from salts_secrets import guests
from salts_secrets import tiers as perm
from salts_secrets.home import data_dir

STATIC = Path(__file__).parent / "static"
# What the demo drafter may share about the fictional owner; a real install starts from board.DEFAULT_POLICY.
DEFAULT_POLICY = """# What may be shared about Salt (read by the drafter, edited by the owner)

## Fine to share
- That she is a pseudonymous on-chain analyst who writes Low Tide; her methods, thresholds and checklists
- Her published calls, post-mortems and opinions, and what she got wrong
- Her field of study and the kind of place she studied (no institution name), her profession in general terms
- The city-level region she is based in (a country or large city is fine, never a street or neighbourhood)
- How she works: what she looks at first, what she refuses to do, how she treats sponsors

## Never share
- Real name, employer, exact address, phone, email, handles other than @saltwater_sats
- Names of family, partner, friends, doctors, contacts, readers, or anyone under NDA
- Accounts, exchanges she uses, wallet addresses, positions, amounts, balances, API keys, file paths
- Exact dates of personal events, health, appointments, schedules
- Unpublished thresholds or anything she marks as private / do not publish
"""
COOKIE = "salt"
OWNER_TTL = 12 * 3600
VIEWER_PREFIX = "salt-v-"  # salts_secrets.readonly blocks every tool in owner sessions with this prefix
VIEWER_TURNS = 12  # then a fresh session, so compression never rotates a read-only session into a new id
VIEWER_CHARS = 2000


class Settings:
    def __init__(self, env: Optional[dict] = None):
        e = os.environ if env is None else env
        self.hermes_url = e.get("SALT_HERMES_URL", "http://127.0.0.1:8642").rstrip("/")
        self.owner_key = e.get("SALT_OWNER_KEY", "")
        self.public_key = e.get("SALT_PUBLIC_KEY", "")
        self.public_profile = e.get("SALT_PUBLIC_PROFILE", "public")
        self.passphrase_hash = e.get("SALT_OWNER_PASSPHRASE_HASH", "")
        self.viewer_hash = e.get("SALT_VIEWER_PASSPHRASE_HASH", "")
        self.viewer_hint = e.get("SALT_VIEWER_PASSPHRASE_SHOWN", "")
        self.secret = (e.get("SALT_SECRET") or secrets.token_hex(32)).encode()
        self.owner_home = Path(e.get("SALT_OWNER_HOME", str(Path.home() / ".hermes")))
        # Draft and Classify call the owner's model through Hermes, which resolves it from this home.
        os.environ.setdefault("HERMES_HOME", str(self.owner_home))
        self.public_home = Path(e.get("SALT_PUBLIC_HOME", str(self.owner_home / "profiles" / self.public_profile)))
        roots = e.get("SALT_WORKSPACE_ROOTS", "")
        self.workspace_roots = [Path(p) for p in roots.split(os.pathsep) if p]
        self.cookie_secure = e.get("SALT_COOKIE_SECURE", "auto")
        self.tier_profiles = {"admin": e.get("SALT_ADMIN_PROFILE", "salt-admin"),
                              "user": e.get("SALT_USER_PROFILE", "salt-user"), "guest": self.public_profile}
        self.tier_keys = {"admin": e.get("SALT_ADMIN_KEY", ""), "user": e.get("SALT_USER_KEY", ""),
                          "guest": self.public_key, "owner": self.owner_key}
        self.owner_name = e.get("SALT_OWNER_NAME", "Lena")
        self.public_name = e.get("SALT_PUBLIC_NAME", "Salt")
        self.turns_per_hour = int(e.get("SALT_TURNS_PER_HOUR", "20"))  # per address, everyone but the owner; 0 = no limit
        if self.turns_per_hour < 0:
            raise ValueError("SALT_TURNS_PER_HOUR must be 0 (no limit) or a positive number of messages")

    def tier_homes(self) -> dict[str, Path]:
        homes = {t: self.owner_home / "profiles" / name for t, name in self.tier_profiles.items()}
        homes["guest"] = self.public_home
        return homes


def render_page(name: str, s: Settings) -> HTMLResponse:
    """A static page with the demo names (Lena, Salt) replaced by the configured ones."""
    stem = name.removesuffix(".html")
    if stem in board.PAGE_NAMES:
        return HTMLResponse(board.render_page(stem, s.owner_name, s.public_name), headers={"Cache-Control": "no-cache"})
    html = board.with_nav((STATIC / name).read_text(encoding="utf-8"), "chat", s.owner_name, hide_owner=True)
    html = re.sub(r"\bLena\b", s.owner_name, html)
    html = re.sub(r"\bSalt\b", s.public_name, html)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


def hash_passphrase(passphrase: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(passphrase.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_passphrase(passphrase: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(passphrase.encode(), salt=bytes.fromhex(salt_hex), n=2 ** 14, r=8, p=1)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def owner_models(owner_home: Path) -> list[dict]:
    """Models the owner can switch between: the config.yaml default first, then each ``providers:`` entry
    that names a model. Non-owner tiers always run their profile's default model."""
    try:
        cfg = yaml.safe_load((owner_home / "config.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    main = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    default = str(main.get("provider") or "")
    models = [{"id": str(k), "model": str(p["model"])} for k, p in (cfg.get("providers") or {}).items()
              if isinstance(p, dict) and p.get("model")]
    if not any(m["id"] == default for m in models):
        models.insert(0, {"id": default or "default", "model": str(main.get("default") or "default")})
    models.sort(key=lambda m: m["id"] != (default or "default"))
    return models


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    s = settings or Settings()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    attempts: dict[str, deque] = defaultdict(deque)

    def sign(data: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()
        return raw + "." + hmac.new(s.secret, raw.encode(), hashlib.sha256).hexdigest()

    def read_state(request: Request) -> dict:
        raw, _, sig = (request.cookies.get(COOKIE) or "").partition(".")
        if raw and hmac.compare_digest(sig, hmac.new(s.secret, raw.encode(), hashlib.sha256).hexdigest()):
            try:
                st = json.loads(base64.urlsafe_b64decode(raw))
                if st.get("owner_exp", 0) < time.time():
                    st["owner_exp"] = 0
                return st
            except ValueError:
                pass
        return {"guest_sid": "", "owner_sid": "", "owner_exp": 0}

    def state(request: Request) -> dict:
        """Cookie state with a stable device id (used to make blocking stick to the device)."""
        st = read_state(request)
        if not st.get("did"):
            st["did"] = secrets.token_hex(12)
        return st

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else ""

    def is_owner(st: dict) -> bool:
        return st.get("owner_exp", 0) > time.time()

    def is_viewer(st: dict) -> bool:
        """Signed in with the read-only passphrase: sees what the owner sees, changes nothing."""
        return not is_owner(st) and st.get("view_exp", 0) > time.time()

    def identity(request: Request, st: dict) -> tuple[str, str]:
        """(tier, member name), re-read every request so board changes apply at once. The owner passphrase
        beats the blocklist (same NAT); a blocked device or IP beats any cookie, including no cookie."""
        if is_owner(st) or is_viewer(st):
            return "owner", ""
        ip = client_ip(request)
        m = perm.member_by_id(s.owner_home, st["member"]) if st.get("member") else None
        if m:
            perm.note_seen(s.owner_home, m["id"], ip, st.get("did", ""))
        if perm.is_blocked(s.owner_home, ip, st.get("did", "")):
            return "blocked", m["name"] if m else ""
        return (m["tier"], m["name"]) if m else ("guest", "")

    def me_body(request: Request, st: dict) -> dict:
        tier, name = identity(request, st)
        body = {"role": tier, "name": name, "label": perm.TIER_LABEL[tier], "owner_until": st.get("owner_exp", 0),
                "guest_access": perm.load(s.owner_home)["guest_access"]}
        if tier == "owner" and is_viewer(st):
            body["readonly"] = True
        elif tier == "owner":
            body["models"] = [{"id": m["id"], "label": m["model"]} for m in owner_models(s.owner_home)]
        return body

    def model_override(tier: str, choice: str) -> dict:
        """Per-request ``provider``/``model`` for Hermes; empty = the profile default."""
        if not choice:
            return {}
        if tier != "owner":
            raise HTTPException(403, "only the owner can switch models")
        models = owner_models(s.owner_home)
        picked = next((m for m in models if m["id"] == choice), None)
        if picked is None:
            raise HTTPException(400, f"unknown model '{choice}'")
        return {} if picked is models[0] else {"provider": picked["id"], "model": picked["model"]}

    def set_cookie(resp, request: Request, st: dict) -> None:
        secure = (s.cookie_secure == "true" or (s.cookie_secure == "auto" and (
            request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")))
        resp.set_cookie(COOKIE, sign(st), httponly=True, samesite="strict", secure=secure, max_age=180 * 86400)

    def respond(request: Request, st: dict, body: dict, status: int = 200) -> JSONResponse:
        resp = JSONResponse(body, status_code=status)
        set_cookie(resp, request, st)
        return resp

    def require_json(request: Request) -> None:
        if "application/json" not in request.headers.get("content-type", ""):
            raise HTTPException(415, "application/json required")

    def require_owner(request: Request) -> dict:
        st = state(request)
        if not is_owner(st):
            raise HTTPException(403, "read-only mode" if is_viewer(st) else "owner only")
        return st

    def require_reader(request: Request) -> dict:
        st = state(request)
        if not (is_owner(st) or is_viewer(st)):
            raise HTTPException(403, "owner only")
        return st

    @app.get("/")
    def index():
        return render_page("index.html", s)

    @app.get("/static/avatar/{name}")
    def avatar_asset(name: str):
        """Portrait images for the chat window (no owner data; plain files under static/avatar)."""
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.(png|jpg|jpeg|webp|svg)", name):
            raise HTTPException(404)
        path = STATIC / "avatar" / name
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, headers={"Cache-Control": "no-cache"})

    @app.get("/shelf")
    def shelf():
        """Context page; guests see demo data, the owner sees the real scan."""
        return render_page("shelf.html", s)

    @app.get("/api/health")
    def health():
        return {"ok": True, "isolation": perm.isolation_check(s.owner_home, s.public_home)}

    @app.get("/api/me")
    def me(request: Request):
        st = state(request)
        return respond(request, st, me_body(request, st))

    @app.post("/api/unlock")
    async def unlock(request: Request):
        require_json(request)
        ip = request.client.host if request.client else "?"
        window = attempts[ip]
        now = time.time()
        while window and window[0] < now - 300:
            window.popleft()
        if len(window) >= 5:
            raise HTTPException(429, "too many attempts, wait 5 minutes")
        window.append(now)
        secret = str((await request.json()).get("passphrase", ""))
        st = state(request)
        if s.passphrase_hash and verify_passphrase(secret, s.passphrase_hash):
            window.clear()
            st["owner_exp"], st["view_exp"] = int(now + OWNER_TTL), 0
            return respond(request, st, me_body(request, st))
        if s.viewer_hash and verify_passphrase(secret, s.viewer_hash):
            window.clear()
            st["owner_exp"], st["view_exp"], st["member"] = 0, int(now + OWNER_TTL), ""
            return respond(request, st, me_body(request, st))
        member = perm.find_member(s.owner_home, secret) if secret else None
        if not member:
            raise HTTPException(401, "wrong passphrase")
        window.clear()
        st["member"], st["owner_exp"] = member["id"], 0
        body = me_body(request, st)
        return respond(request, st, body, 403 if body["role"] == "blocked" else 200)

    @app.post("/api/lock")
    def lock(request: Request):
        require_json(request)
        st = state(request)
        if identity(request, st)[0] == "blocked":
            return respond(request, st, me_body(request, st), 403)
        st["owner_exp"], st["view_exp"], st["member"] = 0, 0, ""
        return respond(request, st, me_body(request, st))

    @app.post("/api/new")
    def new_session(request: Request):
        require_json(request)
        st = state(request)
        st["viewer_sid" if is_viewer(st) else identity(request, st)[0] + "_sid"] = ""
        return respond(request, st, {"ok": True})

    viewer_turns: dict[str, int] = {}
    hourly_turns: dict[str, deque] = defaultdict(deque)

    def spend_turn(request: Request, st: dict):
        """Every chat message costs the owner model tokens; cap each address (or device) per hour.
        Returns a refund for a message Hermes never answered."""
        if not s.turns_per_hour or is_owner(st):
            return lambda: None
        ip = client_ip(request)
        # a proxy/LAN address is shared by every visitor behind it: count per device there
        who = ip if perm.blockable_ip(ip) else "device:" + st["did"]
        now = time.time()
        for k in [k for k, w in hourly_turns.items() if not w or w[-1] < now - 3600]:
            del hourly_turns[k]
        window = hourly_turns[who]
        while window and window[0] < now - 3600:
            window.popleft()
        if len(window) >= s.turns_per_hour:
            wait = int(window[0] + 3600 - now) // 60 + 1
            raise HTTPException(429, f"{s.turns_per_hour} messages an hour reached, try again in {wait} min")
        window.append(now)
        return lambda: now in window and window.remove(now)

    @app.post("/api/chat")
    async def chat(request: Request):
        require_json(request)
        body = await request.json()
        message = str(body.get("message", "")).strip()
        if not message or len(message) > 20000:
            raise HTTPException(400, "message must be 1-20000 chars")
        st = state(request)
        tier = identity(request, st)[0]
        if tier == "blocked":
            raise HTTPException(403, "you are blocked")
        if tier == "guest" and not perm.load(s.owner_home)["guest_access"]:
            raise HTTPException(403, "guest chat is closed; ask the owner for an invite code")
        readonly = is_viewer(st)
        if readonly and len(message) > VIEWER_CHARS:
            raise HTTPException(400, f"read-only mode: messages up to {VIEWER_CHARS} chars")
        override = {} if readonly else model_override(tier, str(body.get("model") or ""))
        sid_key = "viewer_sid" if readonly else tier + "_sid"
        if readonly and viewer_turns.get(st.get(sid_key, ""), 0) >= VIEWER_TURNS:
            st[sid_key] = ""
        if tier == "guest" and st.get(sid_key) in guests.stale_ids(s.public_home):
            st[sid_key] = ""  # a revoke could not delete it: never answer from it again
        if not st.get(sid_key):
            st[sid_key] = (VIEWER_PREFIX if readonly else f"salt-{tier[0]}-") + secrets.token_hex(16)
        if readonly:
            viewer_turns[st[sid_key]] = viewer_turns.get(st[sid_key], 0) + 1
        key = s.tier_keys.get(tier, "")
        if not key:
            if tier in ("admin", "user"):
                raise HTTPException(503, f"the {tier} tier is planned, not implemented yet")
            raise HTTPException(503, f"Hermes key for tier '{tier}' is not configured")
        refund = spend_turn(request, st)
        url = f"{s.hermes_url}/v1/chat/completions" if tier == "owner" else \
            f"{s.hermes_url}/p/{s.tier_profiles[tier]}/v1/chat/completions"
        payload = {"model": "hermes-agent", "stream": True, "messages": [{"role": "user", "content": message}],
                   **override}
        headers = {"Authorization": f"Bearer {key}", "X-Hermes-Session-Id": st[sid_key]}

        async def stream():
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as client:
                    async with client.stream("POST", url, json=payload, headers=headers) as r:
                        if r.status_code != 200:
                            detail = (await r.aread()).decode(errors="replace")[:300]
                            refund()
                            yield _sse({"error": f"Hermes {r.status_code}: {detail}"})
                            return
                        async for line in r.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data)
                            except ValueError:
                                continue
                            for choice in chunk.get("choices") or []:
                                text = (choice.get("delta") or {}).get("content")
                                if text:
                                    yield _sse({"t": text})
                yield _sse({"done": True})
            except httpx.HTTPError as exc:
                refund()
                yield _sse({"error": f"cannot reach Hermes: {type(exc).__name__}"})

        resp = StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        set_cookie(resp, request, st)
        return resp

    board_cache: dict[str, ctx.Item] = {}  # latest scan by id; bodies are served from here

    def board_scan() -> list[ctx.Item]:
        items = ctx.scan(s.owner_home, s.workspace_roots)
        board_cache.clear()
        board_cache.update((it.id, it) for it in items)
        return items

    def json_gz(request: Request, body: dict) -> Response:
        raw = json.dumps(body, ensure_ascii=False).encode()
        if "gzip" in request.headers.get("accept-encoding", "") and len(raw) > 1024:
            return Response(gzip.compress(raw, 5), media_type="application/json",
                            headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
        return Response(raw, media_type="application/json")

    @app.get("/api/board/items")
    def board_items(request: Request):
        """Item list without bodies (sessions alone can be megabytes); bodies come from /api/board/text."""
        require_reader(request)
        items = board_scan()
        allow = ctx.load_allowlist(s.owner_home)
        tiers = classify.cached(s.owner_home, items)
        return json_gz(request, {
            "items": [dict(it.to_dict(with_text=False), status=ctx.status(it, allow),
                           public_text=allow.get(it.id, {}).get("public_text", ""),
                           decided_via=allow.get(it.id, {}).get("decided_via", ""),
                           classification=tiers.get(it.id)) for it in items],
            "owner_home": str(s.owner_home.resolve()), "public_home": str(s.public_home.resolve()),
            "workspace_roots": [str(Path(r).resolve()) for r in s.workspace_roots]})

    @app.get("/api/board/text")
    def board_text(request: Request, id: str):
        require_reader(request)
        it = board_cache.get(id) or {i.id: i for i in board_scan()}.get(id)
        if not it:
            raise HTTPException(404, "item changed or removed; reload the board")
        return {"text": it.text}

    @app.get("/api/board/search")
    def board_search(request: Request, q: str):
        require_reader(request)
        needle = q.strip().lower()
        items = list(board_cache.values()) or board_scan()
        return {"ids": [it.id for it in items if needle in f"{it.title}\n{it.source}\n{it.text}".lower()]}

    @app.post("/api/board/save")
    async def board_save(request: Request):
        require_json(request)
        require_owner(request)
        others = tuple(h for h in s.tier_homes().values() if h.is_dir() and h != s.public_home)
        try:
            return board.save(s.owner_home, s.public_home, s.workspace_roots, board_scan(), await request.json(),
                              other_homes=others)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/board/classify")
    async def board_classify(request: Request):
        """Rules + the owner's model sort every publishable item into A/B/C. Nothing is published here.
        ``rules_only`` (the shelf opening) sends nothing to the model; only the Classify button does."""
        require_json(request)
        st = require_reader(request)
        body = await request.json()
        force = bool(body.get("force"))
        if force and not is_owner(st):
            raise HTTPException(403, "read-only mode")
        ask = None if body.get("rules_only") else classify.host_asker()
        return await classify.classify(s.owner_home, board_scan(), ask, force)

    @app.get("/api/board/guests")
    def board_guests(request: Request):
        """What guests said: live public sessions plus the owner's archive. Owner only; never injected anywhere."""
        require_reader(request)
        return guests.digest(s.owner_home, s.public_home)

    @app.post("/api/board/guests/clear")
    async def board_guests_clear(request: Request):
        require_json(request)
        require_owner(request)
        archived = guests.archive(s.owner_home, s.public_home, reason="owner cleared")
        return {"archived": archived, **guests.purge(s.public_home)}

    @app.get("/api/board/policy")
    def policy_get(request: Request):
        require_reader(request)
        return board.policy_body(s.owner_home, DEFAULT_POLICY)

    @app.post("/api/board/policy")
    async def policy_set(request: Request):
        require_json(request)
        require_owner(request)
        board.save_policy(s.owner_home, str((await request.json()).get("text") or ""))
        return {"ok": True}

    @app.post("/api/board/draft")
    async def board_draft(request: Request):
        require_json(request)
        require_owner(request)
        body = await request.json()
        item_id = str(body.get("id") or "")
        it = board_cache.get(item_id) or {i.id: i for i in board_scan()}.get(item_id)
        if not it:
            raise HTTPException(404, "item changed or removed; reload")
        try:
            return await board.draft(s.owner_home, it, s.public_name, bool(body.get("force")), DEFAULT_POLICY)
        except board.DraftError as exc:
            raise HTTPException(502, str(exc))

    @app.get("/api/board/preview")
    def board_preview(request: Request):
        require_reader(request)
        return ctx.read_projection(s.public_home)

    @app.get("/perm")
    def perm_page(request: Request):
        st = state(request)
        if not (is_owner(st) or is_viewer(st)):
            return RedirectResponse("/")
        return render_page("perm.html", s)

    def deployed() -> dict[str, bool]:
        return {t: h.is_dir() and bool(s.tier_keys.get(t)) for t, h in s.tier_homes().items()}

    def perm_state() -> dict:
        return board.perm_state(s.owner_home, s.tier_homes(), s.tier_profiles, deployed())

    @app.get("/api/perm")
    def perm_get(request: Request):
        require_reader(request)
        return perm_state()

    @app.post("/api/perm/save")
    async def perm_save(request: Request):
        require_json(request)
        require_owner(request)
        synced = board.perm_save(s.owner_home, s.tier_homes(), await request.json())
        return dict(perm_state(), synced=synced)

    @app.post("/api/perm/members")
    async def perm_members(request: Request):
        require_json(request)
        require_owner(request)
        try:
            extra = board.perm_members(s.owner_home, await request.json(), board.planned_tiers(deployed()))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return dict(perm_state(), **extra)

    @app.post("/api/perm/access")
    async def perm_access(request: Request):
        require_json(request)
        require_owner(request)
        try:
            board.perm_access(s.owner_home, await request.json())
        except LookupError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return perm_state()

    @app.get("/guests")
    def guests_page(request: Request):
        st = state(request)
        if not (is_owner(st) or is_viewer(st)):
            return RedirectResponse("/")
        return render_page("guests.html", s)

    @app.get("/doc")
    def doc_page():
        return render_page("doc.html", s)

    @app.get("/api/doc")
    def doc_api():
        return dict(board.doc_facts(s.owner_home, s.public_home), viewer=bool(s.viewer_hash), viewer_hint=s.viewer_hint)

    TRACE_DIR = STATIC / "trace"

    @app.get("/trace")
    def trace_index(request: Request):
        st = state(request)
        if not (is_owner(st) or is_viewer(st)):
            return RedirectResponse("/")
        page = TRACE_DIR / "index.html"
        if not page.is_file():
            return Response("No trace rendered yet: run demo/trace_run.py then demo/trace_render.py", media_type="text/plain")
        return trace_page(page)

    def trace_page(path: Path) -> HTMLResponse:
        html = board.with_nav(path.read_text(encoding="utf-8"), "trace", s.owner_name)
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @app.get("/trace/{name:path}")
    def trace_file(request: Request, name: str):
        require_reader(request)
        target = (TRACE_DIR / name).resolve()
        if not str(target).startswith(str(TRACE_DIR.resolve())) or not target.is_file():
            raise HTTPException(404, "no such trace file")
        if target.suffix == ".html":
            return trace_page(target)
        return FileResponse(target, headers={"Cache-Control": "no-cache"})

    return app


def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "hash-passphrase":
        import getpass
        print(hash_passphrase(getpass.getpass("Owner passphrase: ")))
        sys.exit(0)
    import uvicorn
    uvicorn.run(create_app(), host=os.environ.get("SALT_HOST", "127.0.0.1"),
                port=int(os.environ.get("SALT_PORT", "8780")), proxy_headers=True)
