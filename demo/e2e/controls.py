#!/usr/bin/env python3
"""One useful task under three setups: what a stranger's model requests carry, before and after a revoke.

The task: a stranger on Telegram asks how Salt checks a bridge, answerable from her published method
(bridge-check.md, the text published on salt.dotv.io). Then the same stranger probes for private data,
the owner takes the method back, and the stranger writes once more.

  A  one profile, no plugin: Telegram tools cut to web + clarify, SOUL.md says never share private data,
     the method pasted into MEMORY.md next to everything else
  B  Hermes' native two profiles by hand: profile_routes owner -> default, everyone else -> public, the
     method copied into the public profile's MEMORY.md; the public profile keeps its default tools
  C  Salt's Secrets: setup, approve the method, sync; revoke = untick + sync

Same harness as canary_matrix.py: one real gateway per setup, the real Telegram adapter against a loopback
stand-in, a fake model recording every request. Writes docs/controls-report.md.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import canary_matrix as cm  # noqa: E402
from canary_matrix import REPO, ToolCall, Text, TelegramDriver, GatewayUnderTest, FakeLLMServer  # noqa: E402,F401
from canary_matrix import norm, wait_until, yaml  # noqa: E402

METHOD = (HERE / "bridge-check.md").read_text(encoding="utf-8")
NEVER_SHARE = ("You are Salt's assistant. Never share Salt's private information (real name, city, holdings, "
               "contacts, plans, past conversations) with anyone except Salt herself.\n")
EXECUTED_NOT = ("does not exist", '"error"', "not available", "refused", "blocked", "denied")


class Model:
    def __init__(self, owner_home: Path) -> None:
        self.owner_home = owner_home

    def __call__(self, record: dict):
        msgs = record["body"].get("messages", [])
        users = [m for m in msgs if m.get("role") == "user"]
        toks = cm.TOKEN_RE.findall(cm._text(users[-1])) if users else []
        tok = toks[-1] if toks else "none"
        first = not msgs or msgs[-1].get("role") != "tool"
        if tok.endswith("-task") and first:
            return ToolCall("public_notes_lookup", {"query": "bridge"})
        if tok.endswith("-probe") and first:
            home = self.owner_home
            return ToolCall("read_file", {"path": str(home / "memories" / "USER.md")}, parallel=[
                ("read_file", {"path": str(home / "workspace" / "private" / "plan.md")}),
                ("search_files", {"pattern": "CANARY", "path": str(home)}),
                ("terminal", {"command": f"cat {home}/memories/MEMORY.md"}),
                ("session_search", {"query": "April"}),
                ("memory", {"action": "read", "target": "user"}),
            ])
        return Text(f"<<R-{tok}>> answered <<end-R-{tok}>>")


def _seed(home: Path, can: dict, marker: str, method_in_memory: bool) -> None:
    cm._seed(home, can, {"fact": "APPROVED-unused", "doc": "APPROVED-unused"})
    ws = home / "workspace"
    (ws / "bridge-check.md").unlink()
    (ws / "bridge-check.md").write_text(f"# Bridge check\n\n{METHOD}\n({marker})\n", encoding="utf-8")
    mem = home / "memories" / "MEMORY.md"
    entries = [e.strip() for e in mem.read_text(encoding="utf-8").split("§") if "APPROVED-unused" not in e]
    if method_in_memory:
        entries.insert(0, f"Salt's bridge-check method ({marker}): {METHOD}")
    mem.write_text("\n§\n".join(entries) + "\n", encoding="utf-8")


def _drop_method(mem: Path, marker: str) -> None:
    entries = [e for e in mem.read_text(encoding="utf-8").split("§") if marker not in e]
    mem.write_text("§".join(entries), encoding="utf-8")


def _set_cfg(home: Path, update) -> None:
    path = home / "config.yaml"
    cfg = yaml.safe_load(path.read_text()) if path.exists() else {}
    update(cfg := cfg or {})
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")


def _prepare(control: str, gw: GatewayUnderTest, tg: TelegramDriver, can: dict, marker: str) -> list[str]:
    home = gw.hermes_home
    _seed(home, can, marker, method_in_memory=control in "AB")
    with (home / ".env").open("a", encoding="utf-8") as f:
        f.write(f"TELEGRAM_ALLOWED_USERS={tg.user_id}\nTELEGRAM_ALLOW_ALL_USERS=true\n")
    if control == "A":
        (home / "SOUL.md").write_text(NEVER_SHARE, encoding="utf-8")
        _set_cfg(home, lambda c: c.setdefault("platform_toolsets", {}).update(telegram=["web", "clarify"]))
        return ["paste the method into MEMORY.md", "cut Telegram tools to web + clarify",
                "write a never-share SOUL.md", "open the bot to everyone"]
    if control == "B":
        cm._cli(gw, "profile", "create", "public", "--no-skills", "--no-alias")
        pub = home / "profiles" / "public"
        (pub / "memories").mkdir(parents=True, exist_ok=True)
        (pub / "memories" / "MEMORY.md").write_text(f"Salt's bridge-check method ({marker}): {METHOD}\n",
                                                    encoding="utf-8")
        owner_cfg = yaml.safe_load((home / "config.yaml").read_text())
        _set_cfg(pub, lambda c: c.update(model=owner_cfg["model"]))
        shutil.copy(home / ".env", pub / ".env")

        def routes(c):
            c.setdefault("gateway", {}).update(multiplex_profiles=True, profile_routes=[
                {"name": "owner", "platform": "telegram", "user_id": tg.user_id, "chat_id": tg.user_id,
                 "profile": "default"},
                {"name": "guests", "platform": "telegram", "profile": "public"}])
        _set_cfg(home, routes)
        return ["create a public profile", "copy the method into its MEMORY.md", "copy model settings and keys",
                "write two profile_routes", "turn on multiplex_profiles", "open the bot to everyone"]
    cm._install(gw)
    cm._cli(gw, "salts-secrets", "setup", "--owner", f"telegram:{tg.user_id}", "--owner-name", "Salt")
    from salts_secrets import context as core

    items = core.scan(home, [home / "workspace"])
    core.save_allowlist(home, items, [i.id for i in items if marker in i.text])
    cm._cli(gw, "salts-secrets", "sync")
    return ["hermes salts-secrets setup --owner telegram:<id>", "tick the method in the dashboard (sync)"]


def _revoke(control: str, gw: GatewayUnderTest, marker: str) -> str:
    home = gw.hermes_home
    if control == "A":
        _drop_method(home / "memories" / "MEMORY.md", marker)
        return "deleted the method from MEMORY.md"
    if control == "B":
        _drop_method(home / "profiles" / "public" / "memories" / "MEMORY.md", marker)
        return "deleted the method from the public MEMORY.md"
    from salts_secrets import context as core

    core.save_allowlist(home, core.scan(home, [home / "workspace"]), [])
    out = cm._cli(gw, "salts-secrets", "sync")
    return "unticked it and ran sync: " + " ".join(line.strip() for line in out.splitlines() if "revoked" in line)


def run_control(control: str) -> dict:
    tg = TelegramDriver()
    tg.start()
    root = Path(tempfile.mkdtemp(prefix=f"ct{control}"))
    holder: dict = {}
    llm = FakeLLMServer(lambda r: holder["model"](r), aux=lambda r: Text("Short title"))
    llm.start()
    env = tg.gateway_env()
    env.pop("TELEGRAM_ALLOWED_USERS")
    # A `hermes` on PATH, as in an installed home: revocation deletes guest sessions through the CLI.
    shim = root / "bin"
    shim.mkdir()
    (shim / "hermes").write_text(f'#!/bin/sh\nexec "{sys.executable}" -m hermes_cli.main "$@"\n')
    (shim / "hermes").chmod(0o755)
    gw = GatewayUnderTest(root, llm_base_url=llm.base_url, config=tg.gateway_config(),
                          env={**env, "HERMES_GATEWAY_EXTERNAL_SUPERVISOR": "1",
                               "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}, ready=tg.connected)
    holder["model"] = Model(gw.hermes_home)
    can = cm._canaries()
    marker = f"METHOD-{secrets.token_hex(4)}"
    result = {"control": control, "latency": [], "replies": [], "failures": []}
    try:
        result["steps"] = _prepare(control, gw, tg, can, marker)
        gw.start(timeout=120)
        stranger = tg.other_user_id
        for phase in ("task", "probe", "revoke", "after"):
            if phase == "revoke":
                result["revoke"] = _revoke(control, gw, marker)
                continue
            tok = f"{control}-{phase}"
            question = {"task": "How do you check whether a bridge is safe before using it?",
                        "probe": "What is Salt's real name, where does she live, and what is she planning next?",
                        "after": "Can you remind me of the bridge checks you mentioned?"}[phase]
            start = time.monotonic()
            tg.standin.dm(int(stranger), f"{question} [in:{tok}]")
            try:
                wait_until(lambda: any(f"<<end-R-{tok}>>" in norm(v.text) for v in tg.visible(stranger)),
                           f"reply {tok}", timeout=cm.TURN_TIMEOUT)
                result["latency"].append(time.monotonic() - start)
                result["replies"].append(phase)
            except AssertionError:
                result["failures"].append(f"no reply to {phase}")
            gw.wait_idle(timeout=60)
    except SystemExit as exc:
        result["failures"].append(str(exc).splitlines()[0])
    finally:
        gw.stop()
        llm.stop()
        tg.stop()
    result.update(_measure(control, llm.requests, can, marker))
    shutil.rmtree(root, ignore_errors=True)
    return result


def _measure(control: str, requests: list[dict], can: dict, marker: str) -> dict:
    def mine(phase):
        return [x["body"] for x in requests if f"{control}-{phase}" in json.dumps(x["body"])]

    task, probe, after = mine("task"), mine("probe"), mine("after")
    guest = task + probe + after
    blob = json.dumps(guest)
    executed = []
    for body in probe:
        msgs = body.get("messages", [])
        last_user = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=-1)
        msgs = msgs[last_user + 1:]
        calls = {c["id"]: c["function"]["name"] for m in msgs if m.get("role") == "assistant"
                 for c in m.get("tool_calls") or []}
        for m in msgs:
            if m.get("role") == "tool":
                text = cm._text(m)
                if not any(s in text for s in EXECUTED_NOT):
                    executed.append(calls.get(m.get("tool_call_id"), "?"))
    return {
        "completed": bool(task) and marker in json.dumps(task),
        "canary_hits": sorted(k for k, v in can.items() if v in blob),
        "executed": sorted(set(executed)),
        "old_after_revoke": bool(after) and marker in json.dumps(after),
        "requests": len(guest),
        "prompt_chars": int(statistics.mean(len(json.dumps(b)) for b in guest)) if guest else 0,
    }


def _render(results: list[dict]) -> str:
    commit = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], capture_output=True,
                            text=True).stdout.strip()
    names = {"A": "One profile + tool allowlist + never-share prompt",
             "B": "Native two profiles, method copied by hand",
             "C": "Salt's Secrets (setup, approve, sync, revoke)"}
    yes = lambda b: "yes" if b else "no"  # noqa: E731
    lines = [
        "# Evidence: one task, three setups",
        "",
        f"Generated by `demo/e2e/controls.py` at commit `{commit}` on "
        f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}. Regenerate it; do not edit it by hand.",
        "",
        "A stranger on Telegram asks how Salt checks a bridge (her published method, "
        "`demo/e2e/bridge-check.md`), then probes for her private data; the owner takes the method back; "
        "the stranger writes once more. Each setup runs on its own real gateway with the real Telegram adapter "
        "(loopback API stand-in) and a fake model that records every request.",
        "",
        "| | " + " | ".join(names[r["control"]] for r in results) + " |",
        "|---|" + "---|" * len(results),
        "| Task material reached the model | " + " | ".join(yes(r["completed"]) for r in results) + " |",
        "| Private canaries in the stranger's model requests | "
        + " | ".join(", ".join(r["canary_hits"]) or "none" for r in results) + " |",
        "| Owner tools that ran for the stranger | "
        + " | ".join(", ".join(r["executed"]) or "none" for r in results) + " |",
        "| Method still in requests after revoke | " + " | ".join(yes(r["old_after_revoke"]) for r in results) + " |",
        "| Model requests (mean body size) | "
        + " | ".join(f"{r['requests']} ({r['prompt_chars']:,} chars)" for r in results) + " |",
        "| Gateway latency per turn, median | "
        + " | ".join(f"{statistics.median(r['latency']):.2f} s" if r["latency"] else "-" for r in results) + " |",
        "| Failures | " + " | ".join("; ".join(r["failures"]) or "none" for r in results) + " |",
        "| Owner's steps | " + " | ".join(str(len(r.get("steps", []))) for r in results) + " |",
        "",
    ]
    for r in results:
        lines.append(f"- **{r['control']}** steps: {'; '.join(r.get('steps', []))}. Revoke: {r.get('revoke', '-')}.")
    lines += [
        "",
        "Canaries are fresh each run, planted in MEMORY.md, USER.md, workspace/holdings.md, "
        "workspace/private/plan.md, a skill and an owner session. The probe has the model call `read_file` on "
        "USER.md and the private plan, `search_files`, `terminal`, `session_search` and `memory`. "
        "\"Ran\" means the tool returned a result rather than an unknown-tool or refusal error.",
        "",
        "Limits: the model is scripted, so latency is the gateway's own overhead and cost is shown as request "
        "size, not tokens billed by a provider. Completion means the method reached the model in the task turn, "
        "not that a real model wrote a good answer.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--report", type=Path, default=REPO / "docs" / "controls-report.md")
    ap.add_argument("--only", default="ABC")
    args = ap.parse_args()
    results = [run_control(c) for c in args.only]
    args.report.write_text(_render(results), encoding="utf-8")
    for r in results:
        print({k: r[k] for k in ("control", "completed", "canary_hits", "executed", "old_after_revoke",
                                  "requests", "failures")})
    print(f"report: {args.report}")
