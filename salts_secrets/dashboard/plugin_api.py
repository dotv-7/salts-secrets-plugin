"""Owner-only dashboard API, mounted by Hermes at /api/plugins/salts-secrets/ behind the dashboard auth gate.

The dashboard runs in the owner profile, so the owner home is the current Hermes home and the public
profile is the one named in this plugin's settings. The tab renders the same board pages as the demo
site; their ``/api/...`` calls arrive here with the same paths minus the ``/api`` prefix.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException


def _load_package():
    """Import the plugin directory as a package whatever its install directory is called."""
    root = Path(__file__).resolve().parents[1]
    name = f"_{root.name.replace('-', '_')}_dashboard"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, root / "__init__.py", submodule_search_locations=[str(root)]
        )
        package = importlib.util.module_from_spec(spec)
        sys.modules[name] = package
        spec.loader.exec_module(package)
    modules = ("_yaml", "context", "guests", "tiers", "home", "classify", "board")
    return [importlib.import_module(f"{name}.{m}") for m in modules]


yaml, context, guests, tiers, home, classify, board = _load_package()
NAME = home.NAME

router = APIRouter()


def _settings() -> dict:
    try:
        cfg = yaml.safe_load((home.current() / "config.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        cfg = {}
    entry = ((cfg.get("plugins") or {}).get("entries") or {}).get(NAME) or {}
    return entry.get("settings") or {}


class Install:
    """The owner and public homes and names for one request; the settings can change between requests."""

    def __init__(self):
        settings = _settings()
        self.owner = home.current()
        self.profiles = {"admin": "salt-admin", "user": "salt-user",
                         "guest": str(settings.get("public_profile") or "public")}
        self.public = self.owner / "profiles" / self.profiles["guest"]
        self.workspaces = [Path(p).expanduser() for p in settings.get("workspaces") or [self.owner / "workspace"]]
        self.owner_name = str(settings.get("owner_name") or "the owner")
        self.public_name = str(settings.get("public_name") or self.owner_name)

    def homes(self) -> dict[str, Path]:
        return {t: self.owner / "profiles" / name for t, name in self.profiles.items()}

    def scan(self) -> list:
        return context.scan(self.owner, self.workspaces)

    def item(self, item_id: str):
        found = next((i for i in self.scan() if i.id == item_id), None)
        if found is None:
            raise HTTPException(404, "item changed or was removed; reload")
        return found

    def perm(self) -> dict:
        deployed = {"guest": self.public.is_dir(), "admin": False, "user": False}
        return board.perm_state(self.owner, self.homes(), self.profiles, deployed)


@router.get("/page/{name}")
def page(name: str):
    if name not in board.DASHBOARD_PAGES:
        raise HTTPException(404, "no such page")
    s = Install()
    return {"html": board.render_page(name, s.owner_name, s.public_name, dashboard=True)}


@router.get("/me")
def me():
    return {"role": "owner", "name": Install().owner_name, "label": tiers.TIER_LABEL["owner"],
            "guest_access": tiers.load(home.current())["guest_access"]}


@router.get("/health")
def health():
    s = Install()
    return {"ok": True, "isolation": tiers.isolation_check(s.owner, s.public)}


@router.get("/board/items")
def board_items():
    s = Install()
    return dict(board.items_body(s.owner, s.scan()), owner_name=s.owner_name,
                owner_home=context.display_path(s.owner), public_home=context.display_path(s.public),
                workspace_roots=[context.display_path(r) for r in s.workspaces])


@router.get("/board/text")
def board_text(id: str):
    return {"text": Install().item(id).text}


@router.get("/board/search")
def board_search(q: str):
    return {"ids": board.search(Install().scan(), q)}


@router.post("/board/save")
def board_save(body: dict = Body(...)):
    """Save the allowlist, rewrite the public projection, and purge guest sessions that saw revoked items."""
    s = Install()
    try:
        return board.save(s.owner, s.public, s.workspaces, s.scan(), body, s.owner_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/board/classify")
async def board_classify(body: dict = Body(default={})):
    """Rules + the owner's model sort every publishable item into A/B/C. Nothing is published here.
    ``rules_only`` (the tab opening) sends nothing to the model; only the Classify button does."""
    s = Install()
    ask = None if body.get("rules_only") else classify.host_asker()
    return await classify.classify(s.owner, s.scan(), ask, bool(body.get("force")))


@router.get("/board/guests")
def board_guests():
    s = Install()
    return guests.digest(s.owner, s.public, write=False)


@router.post("/board/guests/clear")
def board_guests_clear():
    s = Install()
    archived = guests.archive(s.owner, s.public, reason="owner cleared")
    return {"archived": archived, **guests.purge(s.public)}


@router.get("/board/policy")
def policy_get():
    return board.policy_body(home.current())


@router.post("/board/policy")
def policy_set(body: dict = Body(...)):
    board.save_policy(home.current(), str(body.get("text") or ""))
    return {"ok": True}


@router.post("/board/policy/rewrite")
async def policy_rewrite(body: dict = Body(...)):
    try:
        return {"text": await board.rewrite_policy(str(body.get("text") or ""), str(body.get("instruction") or ""))}
    except board.DraftError as exc:
        raise HTTPException(502, str(exc))


@router.get("/board/rules")
def rules_get():
    return board.rules_body(home.current())


@router.post("/board/rules")
def rules_set(body: dict = Body(...)):
    try:
        return {"ok": True, **board.save_rules(home.current(), str(body.get("text") or ""))}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/board/draft")
async def board_draft(body: dict = Body(...)):
    """Guest version of one item from the owner's model. Nothing is published here."""
    s = Install()
    try:
        return await board.draft(s.owner, s.item(str(body.get("id") or "")), s.public_name, bool(body.get("force")))
    except board.DraftError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/board/preview")
def board_preview():
    return context.read_projection(Install().public)


@router.get("/perm")
def perm_get():
    return Install().perm()


@router.post("/perm/save")
def perm_save(body: dict = Body(...)):
    s = Install()
    synced = board.perm_save(s.owner, {"guest": s.public}, body)
    return dict(s.perm(), synced=synced)


@router.post("/perm/members")
def perm_members(body: dict = Body(...)):
    s = Install()
    try:
        extra = board.perm_members(s.owner, body, ["admin", "user"])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return dict(s.perm(), **extra)


@router.post("/perm/access")
def perm_access(body: dict = Body(...)):
    s = Install()
    try:
        board.perm_access(s.owner, body)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return s.perm()


@router.get("/doc")
def doc():
    s = Install()
    return dict(board.doc_facts(s.owner, s.public), viewer=False, viewer_hint="")
