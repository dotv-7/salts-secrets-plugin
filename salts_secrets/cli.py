"""``hermes salts-secrets``: set up, check and publish the public side from the owner profile."""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import _yaml as yaml
from . import context, guests, routes, tiers
from .home import NAME, PUBLIC_KEY_ENV, current, data_dir

PLUGIN_DIR = Path(__file__).resolve().parent
TEMPLATES = PLUGIN_DIR / "profile"


def _read_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _hermes(*args: str, profile: str | None = None) -> None:
    """Run the Hermes CLI, so credentials and profiles are written by the code that owns them."""
    cmd = [sys.executable, "-m", "hermes_cli.main"] + (["-p", profile] if profile else []) + list(args)
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)


def _settings(cfg: dict) -> dict:
    entry = cfg.setdefault("plugins", {}).setdefault("entries", {}).setdefault(NAME, {})
    return entry.setdefault("settings", {})


def _enable(cfg: dict) -> None:
    enabled = cfg.setdefault("plugins", {}).setdefault("enabled", [])
    if NAME not in enabled:
        enabled.append(NAME)


def _paths(args: argparse.Namespace) -> tuple[Path, Path, dict]:
    owner = current()
    cfg = _read_yaml(owner / "config.yaml")
    public = args.public or _settings(cfg).get("public_profile") or "public"
    return owner, owner / "profiles" / public, cfg


def _workspaces(owner: Path, cfg: dict) -> list[Path]:
    return [Path(p).expanduser() for p in _settings(cfg).get("workspaces") or [owner / "workspace"]]


SETUP_MARKER = "setup.json"
OWNER_ID = "platform:user_id"


def _owner_ids(values: list[str] | None) -> list[str]:
    out = []
    for value in values or []:
        for part in str(value).split(","):
            part = part.strip().lower()
            platform, _, uid = part.partition(":")
            if not (platform and uid):
                raise SystemExit(f"--owner must look like telegram:123456, got {part!r}")
            out.append(part)
    return out


def leftovers(pub: Path, owner_name: str) -> list[str]:
    """Content in an existing public profile that ``setup`` did not write: a reused profile may already
    hold a SOUL.md, memories or skills that guests would then get."""
    found = []
    soul = pub / "SOUL.md"
    template = (TEMPLATES / "SOUL.md").read_text(encoding="utf-8").format(owner_name=owner_name).strip()
    if soul.exists() and soul.read_text(encoding="utf-8").strip() not in ("", template):
        found.append("SOUL.md differs from the template")
    for path in sorted((pub / "memories").glob("*")) if (pub / "memories").is_dir() else []:
        if path.is_file() and path.read_text(encoding="utf-8", errors="replace").strip():
            found.append(f"memories/{path.name} is not empty")
    skills = sorted(p.parent.name for p in (pub / "skills").rglob("SKILL.md")) if (pub / "skills").is_dir() else []
    if skills:
        found.append(f"skills/ holds {len(skills)} skill(s): {', '.join(skills[:5])}")
    for name in tiers.CONTEXT_FILE_NAMES:
        if (pub / name).exists():
            found.append(f"{name} in the profile root")
    return found


def _reset_public(pub: Path, owner: Path) -> Path:
    """Move leftovers aside (never deleted) and archive + purge any old sessions."""
    backup = data_dir(pub) / f"reset-{int(time.time())}"
    backup.mkdir(parents=True, exist_ok=True)
    for rel in ["SOUL.md", "memories", "skills", *tiers.CONTEXT_FILE_NAMES]:
        if (pub / rel).exists():
            shutil.move(str(pub / rel), str(backup / rel))
    guests.archive(owner, pub, reason="setup --reset-public")
    guests.purge(pub)
    return backup


def _print_checks(checks: list[dict]) -> None:
    for check in checks:
        print(f"  {'ok  ' if check['ok'] else 'FAIL'} {check['text']}")
        if not check["ok"] and check.get("fix"):
            print(f"       fix: {check['fix']}")


def cmd_setup(args: argparse.Namespace) -> int:
    owner, pub, owner_cfg = _paths(args)
    say = lambda text: print("  " + text)  # noqa: E731
    owner_name = args.owner_name or _settings(owner_cfg).get("owner_name") or "the owner"
    owners = _owner_ids(args.owner) or [str(o).lower() for o in _settings(owner_cfg).get("owners") or []]

    print("preflight")
    problems = [c for c in tiers.preflight(owner, after_setup=False) if not c["ok"]]
    if not owners:
        problems.append({
            "ok": False,
            "text": f"no owner identity: pass --owner {OWNER_ID} (repeatable), e.g. --owner telegram:123456",
            "fix": "hermes salts-secrets setup --owner telegram:<your id>",
        })
    if problems:
        _print_checks(problems)
        print("setup stopped: nothing was changed")
        return 2
    say("ok")

    print(f"public profile: profiles/{pub.name}")
    marker = data_dir(pub) / SETUP_MARKER
    if pub.is_dir() and not marker.exists():
        found = leftovers(pub, owner_name)
        if found and not args.reset_public:
            for line in found:
                say("found " + line)
            say("this profile existed before setup; guests would get the content above.")
            say("rerun with --reset-public to move it to plugin-data/salts-secrets/reset-<time>/ and continue")
            return 3
        if found:
            say(f"moved previous content to {_reset_public(pub, owner)}")
    if not pub.is_dir():
        _hermes("profile", "create", pub.name, "--no-skills", "--no-alias")
        say("created")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"created_by": NAME, "at": int(time.time())}), encoding="utf-8")
    soul = pub / "SOUL.md"
    if not soul.exists() or not soul.read_text(encoding="utf-8").strip():
        soul.write_text(
            (TEMPLATES / "SOUL.md").read_text(encoding="utf-8").format(owner_name=owner_name), encoding="utf-8"
        )
        say("SOUL.md written from the template; edit it to shape the public persona")

    cfg = _read_yaml(pub / "config.yaml")
    for key, value in _read_yaml(TEMPLATES / "config.yaml").items():
        cfg[key] = value if not isinstance(value, dict) else {**(cfg.get(key) or {}), **value}
    if owner_cfg.get("model") and "model" not in cfg:
        cfg["model"] = owner_cfg["model"]
        say("model settings copied from the owner profile")
    if owner_cfg.get("providers") and "providers" not in cfg:
        cfg["providers"] = owner_cfg["providers"]
    _enable(cfg)
    _settings(cfg).update(role="public", public_profile=pub.name, owner_name=owner_name)
    if _settings(owner_cfg).get("guest_rate_per_hour"):
        _settings(cfg)["guest_rate_per_hour"] = _settings(owner_cfg)["guest_rate_per_hour"]
    _write_yaml(pub / "config.yaml", cfg)

    target = pub / "plugins" / NAME
    if target.resolve() != PLUGIN_DIR:
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(PLUGIN_DIR, target, ignore=shutil.ignore_patterns("__pycache__", "tests"))
    say(f"{NAME} installed and enabled")

    print("owner profile")
    _enable(owner_cfg)
    owner_cfg.setdefault("gateway", {})["multiplex_profiles"] = True
    _settings(owner_cfg).update(public_profile=pub.name, owner_name=owner_name, owners=owners)
    written = routes.apply(owner_cfg, owners, routes.profile_name(owner), pub.name)
    _write_yaml(owner / "config.yaml", owner_cfg)
    say("gateway.multiplex_profiles on, plugin enabled")
    say("owner identities: " + ", ".join(owners))
    for route in written:
        who = f"owner {route['user_id']}" if route.get("user_id") else "everyone else"
        say(f"profile route: {route['platform']} {who} -> {route['profile']}")
    for platform in sorted({r["platform"] for r in written}):
        # Routed guests are admitted by the receiving bot, i.e. the owner profile's allowlist.
        _hermes("config", "set", routes.ALLOW_ALL_ENV[platform], "true")
        say(f"{routes.ALLOW_ALL_ENV[platform]}=true (the catch-all route takes every non-owner to {pub.name})")

    public_key = tiers.profile_secrets(pub).get("API_SERVER_KEY") or f"salts-public-{secrets.token_hex(16)}"
    _hermes("config", "set", "API_SERVER_KEY", public_key, profile=pub.name)
    _hermes("config", "set", PUBLIC_KEY_ENV, public_key)
    if len(tiers.profile_secrets(owner).get("API_SERVER_KEY", "").strip()) < 16:
        _hermes("config", "set", "API_SERVER_KEY", f"salts-owner-{secrets.token_hex(16)}")
    say(f"public API key stored in both profiles ({PUBLIC_KEY_ENV})")

    print("tiers and projection")
    _sync(owner, pub, owner_cfg, say)
    print()
    result = tiers.isolation_check(owner, pub, gateway=False)
    _print_checks(result["checks"])
    print(result["line"].replace("checks", "checks before restart"))
    if not result["ok"]:
        return 1
    return _next_steps(pub.name)


def _next_steps(public: str) -> int:
    print(
        "\nnext:\n  1. restart the gateway (hermes gateway restart)\n"
        "  2. hermes salts-secrets check   (also proves the running gateway loaded the routing hook)\n"
        "  3. open the dashboard (hermes dashboard) and approve what guests may know under Context"
    )
    return 0


def _sync(owner: Path, pub: Path, owner_cfg: dict, say) -> None:
    for tier, toolsets in tiers.sync(owner, {"guest": pub}).items():
        say(f"{tier}: {', '.join(toolsets)}")
    result = context.publish(
        owner, pub, _workspaces(owner, owner_cfg), _settings(owner_cfg).get("owner_name") or "the owner"
    )
    projection = result["projection"]
    say(f"published {len(projection['facts'])} facts and {len(projection['documents'])} documents")
    if result["revoked"]:
        say(
            f"revoked {result['revoked']} item(s): archived {result['archived']} guest sessions, "
            f"deleted {result['deleted']}, failed {result['failed']}"
        )
    if result.get("error"):
        say("WARNING: " + result["error"])


def cmd_sync(args: argparse.Namespace) -> int:
    owner, pub, owner_cfg = _paths(args)
    _sync(owner, pub, owner_cfg, lambda text: print("  " + text))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    owner, pub, _ = _paths(args)
    result = tiers.isolation_check(owner, pub)
    _print_checks(result["checks"])
    print(result["line"])
    return 0 if result["ok"] else 1


def cmd_guests(args: argparse.Namespace) -> int:
    owner, pub, _ = _paths(args)
    counts = guests.digest(owner, pub)["counts"]
    print(f"{counts['sessions']} guest sessions, {counts['questions']} questions, {counts['probes']} probes")
    print(f"digest: {data_dir(owner) / guests.DIGEST_FILE}")
    return 0


def setup_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--public", help="public profile name (default: public)")
    sub = parser.add_subparsers(dest="salts_command", required=True)
    setup = sub.add_parser("setup", help="create the public profile and install the plugin on both sides")
    setup.add_argument(
        "--owner",
        action="append",
        metavar=OWNER_ID,
        help="your own identity, e.g. telegram:123456 (repeatable; required on first setup)",
    )
    setup.add_argument("--owner-name", help="how the public profile refers to you (a handle, not your real name)")
    setup.add_argument(
        "--reset-public",
        action="store_true",
        help="the public profile already exists with content setup did not write: move it aside and continue",
    )
    setup.set_defaults(salts_handler=cmd_setup)
    sub.add_parser("check", help="verify every isolation condition").set_defaults(salts_handler=cmd_check)
    sub.add_parser("sync", help="publish approved context and tool tiers").set_defaults(salts_handler=cmd_sync)
    sub.add_parser("guests", help="summarise guest conversations").set_defaults(salts_handler=cmd_guests)


def handle(args: argparse.Namespace) -> int:
    return args.salts_handler(args)


def register(ctx) -> None:
    ctx.register_cli_command(
        name=NAME, help="Isolate guests from your private context", setup_fn=setup_parser, handler_fn=handle
    )
