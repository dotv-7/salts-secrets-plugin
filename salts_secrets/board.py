"""The owner board shared by the dashboard tab and the demo site: its pages, the disclosure policy, model
drafts, the permissions view and the developer facts. Callers own auth; every function here is owner-side."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from . import _yaml as yaml
from . import classify, gate, tiers
from . import context as ctx
from .home import data_dir

PAGES = Path(__file__).parent / "dashboard" / "pages"
PAGE_NAMES = ("shelf", "perm", "guests", "doc")
POLICY_FILE = "disclosure_policy.md"
DRAFT_FILE = "public_drafts.json"
DEFAULT_POLICY = """# What may be shared about me (read by the drafter, edited by the owner)

## Fine to share
- My public work, published writing and opinions, and what I got wrong
- My field and profession in general terms
- The country or large city I am based in, never a street or neighbourhood
- How I work: methods, checklists and rules I already state in public

## Never share
- Real name, employer, exact address, phone, email, handles I have not published
- Names of family, partner, friends, doctors, contacts, or anyone under NDA
- Accounts, wallet addresses, positions, amounts, balances, API keys, file paths
- Exact dates of personal events, health, appointments, schedules
- Anything marked private / do not publish
"""


class DraftError(RuntimeError):
    pass


def render_page(name: str, owner_name: str, public_name: str, dashboard: bool = False) -> str:
    """A board page with its top bar and the demo names (Lena, Salt) replaced by the configured ones."""
    if name not in PAGE_NAMES:
        raise KeyError(name)
    if dashboard and name == "doc":  # the installed plugin explains itself; the demo site keeps its API doc
        return with_nav((PAGES / "guide.html").read_text(encoding="utf-8"), name, owner_name, dashboard)
    html = with_nav((PAGES / f"{name}.html").read_text(encoding="utf-8"), name, owner_name, dashboard)
    html = re.sub(r"\bLena\b", owner_name, html)
    return re.sub(r"\bSalt\b", public_name, html)


def policy_text(owner_home: Path, default: str = DEFAULT_POLICY) -> str:
    try:
        return (data_dir(owner_home) / POLICY_FILE).read_text(encoding="utf-8")
    except OSError:
        return default


def policy_body(owner_home: Path, default: str = DEFAULT_POLICY) -> dict:
    return {"text": policy_text(owner_home, default), "path": str((data_dir(owner_home) / POLICY_FILE).resolve())}


def save_policy(owner_home: Path, text: str) -> None:
    data_dir(owner_home).mkdir(parents=True, exist_ok=True)
    (data_dir(owner_home) / POLICY_FILE).write_text(text.strip() + "\n", encoding="utf-8")


def _draft_system(kind: str, public_name: str, policy: str) -> str:
    base = ("You produce the public version of one piece of a private assistant's context. The assistant belongs to "
            f"{public_name}; strangers will read what you write through the public assistant. Follow the "
            "owner's disclosure policy below exactly: keep what it allows, remove or generalise what it forbids. Do not add "
            "facts. Write in the language of the source. No preamble, no quotes, no commentary. If nothing in the source can "
            "be shared under the policy, reply with exactly: NOTHING_SAFE_TO_SHARE\n\n")
    if kind == "skill":
        shape = ("The source is a SKILL.md: a reusable method. Output a complete public skill in markdown with the same "
                 "headings, keeping every step, rule and threshold that the owner's published writing already states. Drop "
                 "the YAML front matter. Remove any section marked private or 'do not publish' entirely, along with accounts, "
                 "paths, people, positions and unpublished thresholds. The result must be usable by a stranger who wants to "
                 "apply the method to their own case.\n\n")
    else:
        shape = ("The source is one entry of the owner's own context: a memory line, a profile fact or a file. Rewrite "
                 "that entry itself into its public version, in the source's voice, person, tense, form and order: a terse "
                 "memory line stays a terse line in the same style, a list stays a list, a rule stays a rule. It remains "
                 "the assistant's own note about its owner, not a report on it: never write 'The note', 'This entry', "
                 "'The source', 'The user' or say what the text is about. Shorten only by removing or generalising what "
                 "the policy forbids.\n\n")
    return base + shape + "OWNER'S DISCLOSURE POLICY:\n" + policy


def _draft_store(owner_home: Path) -> dict:
    try:
        return json.loads((data_dir(owner_home) / DRAFT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


async def draft(owner_home: Path, item: ctx.Item, public_name: str, force: bool = False,
                default_policy: str = DEFAULT_POLICY) -> dict:
    """The owner's model drafts the public version of *item*, cached per item until its text changes."""
    store = _draft_store(owner_home)
    user = f"Source: {item.group} › {item.title}\n\n{item.text[:12000]}"
    system = _draft_system(item.kind, public_name, policy_text(owner_home, default_policy))
    asked = hashlib.sha256((system + user).encode()).hexdigest()[:16]
    hit = store.get(item.id)
    if hit and hit.get("asked") == asked and not force:
        return {"draft": hit["draft"], "cached": True, "tokens": hit.get("tokens", 0)}
    try:
        result = await classify.host_llm().acomplete(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.2, max_tokens=2000 if item.kind == "skill" else 800, purpose="salts-secrets.draft",
        )
    except Exception as exc:
        raise DraftError(f"model call failed: {type(exc).__name__}") from exc
    text, tokens = (result.text or "").strip(), int(result.usage.total_tokens or 0)
    if not text:
        raise DraftError("model returned an empty draft (reasoning used up the budget?); not cached")
    store[item.id] = {"asked": asked, "draft": text, "tokens": tokens, "at": int(time.time())}
    try:
        data_dir(owner_home).mkdir(parents=True, exist_ok=True)
        tmp = data_dir(owner_home) / (DRAFT_FILE + ".tmp")
        tmp.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(data_dir(owner_home) / DRAFT_FILE)
    except OSError:
        pass
    return {"draft": text, "cached": False, "tokens": tokens}


def items_body(owner_home: Path, items: list[ctx.Item]) -> dict:
    allow = ctx.load_allowlist(owner_home)
    tiers_by_id = classify.cached(owner_home, items)
    return {"items": [dict(it.to_dict(with_text=False), status=ctx.status(it, allow),
                           public_text=allow.get(it.id, {}).get("public_text", ""),
                           decided_via=allow.get(it.id, {}).get("decided_via", ""),
                           classification=tiers_by_id.get(it.id)) for it in items]}


def search(items: list[ctx.Item], q: str) -> list[str]:
    needle = q.strip().lower()
    return [it.id for it in items if needle in f"{it.title}\n{it.source}\n{it.text}".lower()]


def save(owner_home: Path, public_home: Path, workspace_roots: list[Path], items: list[ctx.Item], body: dict,
         owner_name: str = "the owner", other_homes: tuple[Path, ...] = ()) -> dict:
    """Approve what the board ticked, then publish; publishing purges guest sessions that saw a revoked item."""
    texts, decisions = body.get("public_texts") or {}, body.get("decisions") or {}
    if not isinstance(texts, dict) or not isinstance(decisions, dict):
        raise ValueError("public_texts and decisions must be objects")
    approved = ctx.approve(owner_home, items, [str(i) for i in body.get("public_ids") or []],
                           {str(k): str(v) for k, v in texts.items()},
                           {str(k): v for k, v in decisions.items() if isinstance(v, dict)})
    for home in other_homes:
        ctx.publish(owner_home, home, workspace_roots, owner_name)
    purge = ctx.publish(owner_home, public_home, workspace_roots, owner_name)
    projection = purge.pop("projection")
    purge.update(rejected=approved["rejected"], combination=approved["combination"])
    return {"facts": len(projection["facts"]), "documents": len(projection["documents"]), **purge}


def planned_tiers(deployed: dict[str, bool]) -> list[str]:
    """Admin and User are designed but not shipped without their own profile, so they stay greyed out."""
    return [t for t in ("admin", "user") if not deployed.get(t)]


def perm_state(owner_home: Path, homes: dict[str, Path], profiles: dict[str, str], deployed: dict[str, bool]) -> dict:
    policy = tiers.load(owner_home)
    return {
        "guest_access": policy["guest_access"],
        "blocklist": [{k: b.get(k) for k in ("kind", "value", "name", "since", "until")} for b in policy["blocklist"]],
        "audit": {t: tiers.audit(homes[t]) for t in tiers.CONFIGURABLE},
        "tiers": list(tiers.TIERS), "labels": tiers.TIER_LABEL, "policy": policy["tiers"],
        "catalog": [{"id": k, "tools": list(v[0]), "category": v[1], "label": v[2], "locked": v[3], "warn": v[4]}
                    for k, v in tiers.CATALOG.items()],
        "members": [tiers.public_member(m) for m in policy["members"]],
        "profiles": {t: {"name": profiles[t], "home": str(homes[t].resolve()), "deployed": deployed.get(t, False)}
                     for t in tiers.CONFIGURABLE},
        "policy_file": str((owner_home / tiers.POLICY_FILE).resolve()),
        "planned": planned_tiers(deployed),
    }


def perm_save(owner_home: Path, homes: dict[str, Path], body: dict) -> dict:
    policy = tiers.load(owner_home)
    policy["tiers"] = body.get("tiers") or {}
    tiers.save(owner_home, policy)
    return tiers.sync(owner_home, homes)


def perm_members(owner_home: Path, body: dict, planned: list[str]) -> dict:
    """Add, retier or delete a member; returns ``{"code": ..., "added": ...}`` on add. ValueError on a bad request."""
    action, tier = body.get("action"), body.get("tier")
    if action in ("add", "update") and tier in planned:
        raise ValueError("Admin and User tiers are planned, not implemented yet")
    if action == "add":
        member, code = tiers.add_member(owner_home, str(body.get("name", "")), str(tier or ""))
        return {"code": code, "added": tiers.public_member(member)}
    if action in ("update", "delete") and tiers.update_member(
            owner_home, str(body.get("id", "")), tier, delete=action == "delete"):
        return {}
    raise ValueError("bad request")


def perm_access(owner_home: Path, body: dict) -> None:
    if body.get("action") == "unblock":
        if not tiers.unblock(owner_home, str(body.get("kind", "")), str(body.get("value", ""))):
            raise LookupError("no such block")
    elif body.get("action") == "guest_access":
        tiers.set_guest_access(owner_home, bool(body.get("enabled")))
    else:
        raise ValueError("bad request")


def doc_facts(owner_home: Path, public_home: Path) -> dict:
    """What this install actually offers a stranger, read from its own config and projection."""
    proj = ctx.read_projection(public_home)
    try:
        cfg = yaml.safe_load((owner_home / "config.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        cfg = {}
    entry = ((cfg.get("plugins") or {}).get("entries") or {}).get("salts-secrets") or {}
    owners = (entry.get("settings") or {}).get("owners") or []
    platforms = sorted({str(o).split(":", 1)[0].capitalize() for o in owners if ":" in str(o)})
    return {"facts": len(proj.get("facts") or []),
            "documents": [d.get("title", "") for d in proj.get("documents") or []],
            "tools": sorted(gate.allowed_tools(public_home)), "platforms": platforms,
            "guest_access": tiers.load(owner_home)["guest_access"],
            "isolation": tiers.isolation_check(owner_home, public_home).get("line", "")}


# One top bar for every page, same links in the same order; the current page is marked, never removed.
NAV_LINKS = (("chat", "/", "CHAT"), ("shelf", "/shelf", "CONTEXT"), ("perm", "/perm", "PERMISSIONS"),
             ("guests", "/guests", "GUESTS"), ("trace", "/trace", "TRACE"), ("doc", "/doc", "DOC"))
NAV_TITLE = {"chat": "CHAT", "shelf": "CONTEXT", "perm": "PERMISSIONS", "guests": "WHAT GUESTS ASKED",
             "trace": "TRACE", "doc": "DEVELOPER DOC"}
DASHBOARD_PAGES = ("shelf", "perm", "guests", "doc")  # the chat and the trace stay on the demo site
OWNER_ONLY = ("shelf", "perm", "guests", "trace")
NAV_CSS = """<style>
html,body{overscroll-behavior:none}
body::before{z-index:60}
nav.salt-nav{position:sticky;top:0;z-index:40;height:64px;margin:0;padding:0 calc(53px - (100vw - 100%)) 0 36px;display:flex;align-items:center;
justify-content:space-between;gap:24px;background:var(--nav-bg,#f2f2f2);color:var(--nav-fg,#0000f2);
border-bottom:1px solid var(--nav-hair,rgba(0,0,242,.18));box-sizing:border-box;flex-wrap:nowrap}
nav.salt-nav .l,nav.salt-nav .r{display:flex;align-items:center;gap:16px;min-width:0}
nav.salt-nav .r{gap:26px;flex-shrink:0}
nav.salt-nav .line{width:40px;height:1px;background:currentColor;flex-shrink:0}
nav.salt-nav .lbl{font:500 10.5px/1 var(--nav-font,"JetBrains Mono",ui-monospace,Menlo,monospace);letter-spacing:.12em;
text-transform:uppercase;color:inherit;white-space:nowrap}
nav.salt-nav .r a{border:0;padding:4px 0;background:none;color:inherit;text-decoration:none;cursor:pointer}
nav.salt-nav .r a:hover{text-decoration:underline;text-underline-offset:6px}
nav.salt-nav .r a.on{font-weight:700;text-decoration:underline;text-decoration-thickness:2px;text-underline-offset:6px}
nav.salt-nav.hide-own .r a.own{visibility:hidden}
@media (max-width:820px){nav.salt-nav{padding:0 16px}nav.salt-nav .r{gap:14px}nav.salt-nav .line{display:none}}
</style>"""
_NAV_BLOCK = re.compile(r"<!--SALT-NAV-->(.*?)<!--/SALT-NAV-->", re.DOTALL)
_WEB_FONTS = re.compile(r"<link[^>]+fonts\.(?:googleapis|gstatic)\.com[^>]*>")
# Inside the Hermes dashboard: API calls go to this plugin's routes through the plugin SDK's authedFetch (it
# applies the base path and the loopback-token or gated-cookie auth), links switch the tab's page, and the
# fonts come from the plugin's own bundle instead of Google.
DASHBOARD_SHIM = """<script>(function(){
var P=window.parent,S=P!==window&&P.__HERMES_PLUGIN_SDK__;if(!S||!S.authedFetch)return;
var B='/api/plugins/salts-secrets',F=window.fetch.bind(window);
window.fetch=function(u,i){if(typeof u==='string'&&u.indexOf('/api/')===0)return S.authedFetch(B+u.slice(4),i);return F(u,i);};
var PAGES={'/shelf':'shelf','/perm':'perm','/guests':'guests','/doc':'doc'};
document.addEventListener('click',function(e){var a=e.target.closest&&e.target.closest('a[href]');if(!a)return;
var href=a.getAttribute('href');if(href.charAt(0)!=='/'||href.charAt(1)==='/')return;e.preventDefault();
var p=PAGES[href.split('?')[0]];if(p&&P.__saltsSecretsNav)P.__saltsSecretsNav(p);},true);
})();</script>
<link rel="stylesheet" href="__SALT_ASSETS__/dist/fonts/fonts.css">"""


def nav_html(page: str, owner_name: str, tools: str = "", dashboard: bool = False, hide_owner: bool = False) -> str:
    links = "".join(
        f'<a class="lbl{" on" if key == page else ""}{" own" if key in OWNER_ONLY else ""}" href="{href}"'
        f'{" aria-current=page" if key == page else ""}>{label}</a>'
        for key, href, label in NAV_LINKS if not dashboard or key in DASHBOARD_PAGES)
    who = owner_name.upper() if owner_name != "Lena" else "LENA MOREAU"
    if page == "chat":
        who = "SALT'S ASSISTANT"  # the chat page sets the owner's name once it knows who signed in
    return (f'{NAV_CSS}<nav class="salt-nav top bar{" hide-own" if hide_owner else ""}"><div class="l">'
            f'<span class="lbl" id="brandWho">{who}</span>'
            f'<span class="line"></span><span class="lbl">{"GUIDE" if dashboard and page == "doc" else NAV_TITLE[page]}</span>'
            f'{tools}</div>'
            f'<div class="r">{links}</div></nav>')


def with_nav(html: str, page: str, owner_name: str, dashboard: bool = False, hide_owner: bool = False) -> str:
    html = _NAV_BLOCK.sub(lambda m: nav_html(page, owner_name, m.group(1), dashboard, hide_owner), html, count=1)
    if dashboard:
        html = _WEB_FONTS.sub("", html).replace("<head>", "<head>" + DASHBOARD_SHIM, 1)
    return html
