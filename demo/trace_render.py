#!/usr/bin/env python3
"""Render a trace bundle (trace_run.py) as one page: the same questions, two sessions side by side, every
token-bearing piece verbatim: stored system prompt, exact tool schemas, every message row, API usage per call.
Within a turn the two sequences are aligned step by step (tool call next to tool call, result next to result);
where one side has no matching step the other side faces an empty slot. No scrollbars inside blocks: a block
longer than the fold limit shows its head and opens in place.

usage: trace_render.py <bundle.json> <out_dir>   -> <out_dir>/index.html, run_<id>.html, run_<id>_{owner,public}.prompt.txt
"""
import html
import json
import re
import sys
from pathlib import Path

HERMES = "Hermes Agent v0.23.0 (local 2bd378ed, hermes-latest)"
MODEL_NOTE = "deepseek-flash as the API reports it: the DeepSeek Flash build of 2026-10-17, set in Lena's config.yaml, the same model on both sides; chain of thought is removed before publishing"
FOLD = {"sys": 14000, "tools": 6000, "tool": 7000, "think": 14000, "asst": 14000, "call": 6000, "user": 14000, "note": 2000}

CSS = """
:root{--paper:#f2f2f2;--sheet:#fdfdfd;--ink:#000091;--muted:#6b6bd6;--blue:#0000f2;--frame:#7f7fff;--hair:rgba(0,0,242,.18);--sel:#e6e6f4;
--sans:"Inter Tight",system-ui,sans-serif;--mono:"JetBrains Mono",ui-monospace,Menlo,monospace;--disp:"Antonio",Impact,sans-serif;--serif:"EB Garamond",Georgia,serif}
*{box-sizing:border-box}html,body{margin:0}
body{background:var(--paper);color:var(--ink);font-family:var(--sans);-webkit-font-smoothing:antialiased}
body::before{content:"";position:fixed;inset:0;border:6px solid var(--frame);pointer-events:none;z-index:50}
body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:49;opacity:.10;background:radial-gradient(circle at 50% 50%,rgba(0,0,242,.35) 0 .5px,transparent 1px) 0 0/4px 4px}
.lbl{font:500 10.5px/1 var(--mono);letter-spacing:.12em;text-transform:uppercase}
a{color:inherit;text-decoration:none}
.top{position:sticky;top:0;height:64px;padding:0 36px;display:flex;align-items:center;justify-content:space-between;color:var(--blue);z-index:30;background:var(--paper);border-bottom:1px solid var(--hair)}
.top .l,.top .r{display:flex;align-items:center;gap:16px}.top .r{gap:26px}.top .line{width:40px;height:1px;background:currentColor}
.top a:hover{text-decoration:underline;text-underline-offset:4px}
.wrap{display:grid;grid-template-columns:360px minmax(0,1fr);gap:0}
.side{position:sticky;top:64px;height:calc(100vh - 64px);overflow:auto;border-right:1px solid var(--hair);padding:18px 16px 40px;background:var(--paper)}
.side .row{display:grid;grid-template-columns:26px 1fr 1fr;gap:4px;align-items:stretch;margin:2px 0}
.side .row.head{margin:0 0 6px;color:var(--blue)}.side .row.head .lbl{font-size:9px}
.side .row .n{font:400 9.5px/1 var(--mono);color:var(--muted);padding-top:5px;text-align:right;padding-right:4px}
.side .none{display:block;border:1px dashed rgba(0,0,242,.35);color:var(--muted);text-align:center;font:400 10px/1 var(--mono);padding:4px 0}
.side .turn{display:grid;grid-template-columns:auto 1fr auto;gap:8px;align-items:baseline;margin:14px 0 4px;padding:6px 6px;border-top:1px solid var(--blue);color:var(--blue)}
.side .turn .q{font:400 12px/1.3 var(--serif);color:var(--ink);overflow:hidden;white-space:nowrap;text-overflow:ellipsis}.side .turn .cnt{font:400 9.5px/1 var(--mono);color:var(--muted)}
.side .turn:hover .q{white-space:normal}
.side .seg{display:flex;justify-content:space-between;gap:4px;padding:3px 5px;border-left:2px solid transparent;color:var(--ink);white-space:nowrap;overflow:hidden;font:400 10px/1.4 var(--mono);background:var(--sheet);border-top:1px solid var(--hair);border-right:1px solid var(--hair);border-bottom:1px solid var(--hair);min-width:0}
.side .seg:hover{background:var(--sel)}.side .seg b{font-weight:500;overflow:hidden;text-overflow:ellipsis}.side .seg span{color:var(--muted);flex:none}
.side .seg.sys{border-color:#047857}.side .seg.tools{border-color:#0ea5e9}.side .seg.user{border-color:var(--blue)}.side .seg.think{border-color:#a855f7}.side .seg.call{border-color:#f97316}.side .seg.tool{border-color:#b45309}.side .seg.asst{border-color:#1a1a1a}.side .seg.note{border-color:#9ca3af}
.side .key{margin-top:14px;font:400 10px/1.6 var(--mono);color:var(--muted)}
.main{padding:28px 36px 90px;min-width:0}
.hero .eyebrow{color:var(--blue);display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.hero h1{margin:12px 0 14px;font:400 clamp(44px,6.5vw,96px)/.9 var(--disp);letter-spacing:.005em;text-transform:uppercase;color:var(--blue)}
.hero .lede{font:400 17px/1.5 var(--serif);max-width:80ch;color:var(--ink)}
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px 24px;margin:18px 0 6px;font:400 11px/1.6 var(--mono);color:var(--muted)}
.meta div{min-width:0;overflow-wrap:anywhere}.meta b{color:var(--blue);font-weight:500}
.heads{position:sticky;top:64px;z-index:20;background:var(--paper);display:grid;grid-template-columns:1fr 1fr;gap:22px;padding:14px 0 10px;border-bottom:1px solid var(--blue);margin-top:18px}
.heads .who{font:400 28px/1 var(--disp);text-transform:uppercase;color:var(--blue)}.heads .sub{margin-top:5px;font:400 10px/1.5 var(--mono);color:var(--muted);overflow-wrap:anywhere}
.heads .r{text-align:right}
.band{margin:26px 0 8px;padding:10px 14px;border:1px solid var(--blue);background:var(--sel);display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
.band .lbl{color:var(--blue)}.band .q{font:400 16px/1.4 var(--serif);color:var(--ink)}
.diff{margin:8px 0 10px;padding:10px 14px;border:1px dashed rgba(0,0,242,.45);font:400 11px/1.6 var(--mono);color:var(--ink);display:grid;grid-template-columns:1fr 1fr;gap:22px}
.diff b{color:var(--blue);font-weight:500;display:block;margin-bottom:2px}
.pair{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:22px;align-items:stretch;margin-bottom:10px}
.cell{min-width:0;display:flex;flex-direction:column}
.cell .seg{flex:1}
.gap{flex:1;border:1px dashed rgba(0,0,242,.3);display:flex;align-items:center;justify-content:center;color:var(--muted);font:400 10px/1 var(--mono);letter-spacing:.12em;text-transform:uppercase;min-height:44px}
.seg{border:1px solid var(--hair);background:var(--sheet)}
.seg .h{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:6px 10px;border-bottom:1px solid var(--hair);font:500 9.5px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--blue)}
.seg .h .t{color:var(--muted);font-weight:400;letter-spacing:.06em;text-transform:none}
.seg .h i{display:inline-block;width:8px;height:8px;margin-right:8px;vertical-align:-1px}
.seg.sys .h i{background:#047857}.seg.tools .h i{background:#0ea5e9}.seg.user .h i{background:var(--blue)}.seg.think .h i{background:#a855f7}.seg.call .h i{background:#f97316}.seg.tool .h i{background:#b45309}.seg.asst .h i{background:#1a1a1a}.seg.note .h i{background:#9ca3af}
.seg pre{margin:0;padding:10px 12px;white-space:pre-wrap;overflow-wrap:anywhere;font:400 11.5px/1.5 var(--mono);color:#1a1a1a}
.seg.asst pre{color:var(--ink)}.seg.think pre{color:#5b21b6;background:#faf5ff}.seg.user pre{color:var(--blue);font-size:12.5px}
.seg .fold{display:none}.seg.open .fold{display:inline}.seg .more{display:block;width:100%;border:0;border-top:1px solid var(--hair);background:#fff;color:var(--blue);font:500 9.5px/1 var(--mono);letter-spacing:.12em;text-transform:uppercase;padding:8px;cursor:pointer}
.usage{font:400 10.5px/1.6 var(--mono);color:var(--muted);padding:6px 2px 2px;display:flex;flex-wrap:wrap;gap:4px 16px}.usage b{color:var(--blue);font-weight:500}
.foot{display:flex;justify-content:space-between;color:var(--blue);padding:40px 0 0}
@media (max-width:1000px){.wrap{grid-template-columns:1fr}.side{position:static;height:auto;border-right:0;border-bottom:1px solid var(--hair)}.pair,.heads,.diff{grid-template-columns:1fr}.heads .r{text-align:left}}
"""

TOP = "<!--SALT-NAV--><!--/SALT-NAV-->"  # the demo server puts the shared top bar here


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def fmt(n) -> str:
    return f"{n:,}" if isinstance(n, int) else "—"


def seg(kind: str, title: str, body: str, tok=None, extra: str = "", anchor: str = "") -> str:
    t = f'<span class="t">{esc(extra)}</span>' if extra else ""
    tokhtml = f'<span class="t">{fmt(tok)} tok</span>' if tok is not None else ""
    body = body if body else "(empty)"
    limit = FOLD.get(kind, 8000)
    if len(body) > limit:
        cut = body.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        inner = (f'<pre>{esc(body[:cut])}<span class="fold">{esc(body[cut:])}</span></pre>'
                 f'<button class="more" onclick="var s=this.parentElement;s.classList.toggle(\'open\');this.textContent=s.classList.contains(\'open\')?\'Show less\':\'Show all · {len(body):,} chars\'">Show all · {len(body):,} chars</button>')
    else:
        inner = f"<pre>{esc(body)}</pre>"
    aid = f' id="{anchor}"' if anchor else ""
    return f'<div class="seg {kind}"{aid}><div class="h"><span><i></i>{esc(title)} {t}</span>{tokhtml}</div>{inner}</div>'


def rows_to_turns(rows: list[dict], questions: list[str]) -> list[list[dict]]:
    qset = {q.strip() for q in questions}
    turns: list[list[dict]] = []
    cur: list[dict] = []
    for r in rows:
        if r["role"] == "user" and not r.get("tool_call_id") and (r.get("content") or "").strip() in qset:
            if cur:
                turns.append(cur)
            cur = [r]
        else:
            cur.append(r)
    if cur:
        turns.append(cur)
    return turns


def row_segments(r: dict) -> list[tuple[str, str, str, object, str]]:
    """(kind, title, body, tok, extra) pieces of one state.db row, in the order the model produced/received them."""
    out = []
    role, tok = r["role"], r.get("token_count")
    if role == "user":
        out.append(("user", "user", r.get("content") or "", None, f"state.db row {r['id']}"))
    elif role == "assistant":
        if r.get("reasoning_content"):
            out.append(("think", "assistant · reasoning", r["reasoning_content"], None, f"row {r['id']} · reasoning_content, the model's chain of thought"))
        if r.get("tool_calls"):
            out.append(("call", "assistant · tool_calls", r["tool_calls"], tok if not r.get("content") else None, f"row {r['id']} · tool_calls, raw JSON as stored"))
        if r.get("content"):
            out.append(("asst", "assistant", r["content"], tok, f"row {r['id']}"))
    elif role == "tool":
        body = r.get("api_content") if r.get("api_content") else (r.get("content") or "")
        out.append(("tool", f"tool result · {r.get('tool_name') or ''}", body, None,
                    f"row {r['id']} · Hermes ran the tool and appended this as role=tool" + (" (api_content)" if r.get("api_content") else "")))
    else:
        out.append(("note", role, r.get("content") or "", None, f"row {r['id']} · {r.get('display_kind') or ''}"))
    return out


def align(left: list, right: list) -> list[tuple]:
    """LCS on step kinds: same kinds sit on the same row; unmatched steps face an empty slot."""
    n, m = len(left), len(right)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            dp[i][j] = dp[i + 1][j + 1] + 1 if left[i][0] == right[j][0] else max(dp[i + 1][j], dp[i][j + 1])
    rows, i, j = [], 0, 0
    while i < n or j < m:
        if i < n and j < m and left[i][0] == right[j][0]:
            rows.append((left[i], right[j]))
            i, j = i + 1, j + 1
        elif j < m and (i >= n or dp[i][j + 1] >= dp[i + 1][j]):
            rows.append((None, right[j]))
            j += 1
        else:
            rows.append((left[i], None))
            i += 1
    return rows


def prompt_txt(side: dict) -> str:
    parts = ["=== SYSTEM PROMPT (state.db system_prompts) ===", side["system_prompt"], "",
             "=== TOOLS (exact schemas sent on every call) ===", json.dumps(side["tools"], ensure_ascii=False), ""]
    for r in side["messages"]:
        for kind, title, body, tok, extra in row_segments(r):
            parts += [f"=== {title} · {extra} ===", body, ""]
    return "\n".join(parts)


LOOPBACK = re.compile(r"https?://(?:127\.0\.0\.1|localhost|0\.0\.0\.0)(?::\d+)?")


def redact(bundle: dict) -> dict:
    """Drop chain of thought and replace machine paths and loopback URLs before anything is published."""
    sides = bundle.get("sides") or {}
    homes = [(str((sides.get("public") or {}).get("home") or ""), "<HERMES_HOME>/profiles/public"),
             (str((sides.get("owner") or {}).get("home") or ""), "<HERMES_HOME>"), (str(Path.home()), "~")]
    swaps = sorted({(old, new) for old, new in homes if old}, key=lambda kv: -len(kv[0]))

    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k != "reasoning_content"}
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, str):
            for old, new in swaps:
                value = value.replace(old, new)
            return LOOPBACK.sub("<local-api>", value)
        return value

    return clean(bundle)


def render(bundle: dict, out_dir: Path) -> Path:
    bundle = redact(bundle)
    L, R = bundle["sides"]["owner"], bundle["sides"]["public"]
    qs = bundle["questions"]
    qps = bundle.get("questions_public") or qs
    tl, tr = rows_to_turns(L["messages"], qs + qps), rows_to_turns(R["messages"], qs + qps)

    def qtext(i):
        return qs[i] if qps[i] == qs[i] else f"{qs[i]}  ⇄  {qps[i]}"

    def steps(rows, key):
        return [(k, t, b, tok, ex, f"r{r['id']}-{key}") for r in rows for (k, t, b, tok, ex) in row_segments(r)]

    def tools_block(tools, side_name):
        body = "\n".join(json.dumps(t, ensure_ascii=False) for t in tools)
        return seg("tools", f"tools · {len(tools)} schemas", body, None, f"{side_name} · one JSON object per tool, exactly as sent")

    def ses(side):
        return side["session"]

    model_api = next((c.get("model") for t in bundle["turns"] for c in t["calls"].values() if c.get("model")), "") or L["session"].get("model")
    aligned_turns = [align(steps(tl[i] if i < len(tl) else [], "L"), steps(tr[i] if i < len(tr) else [], "R")) for i in range(len(qs))]

    # Sidebar: one outline for both sessions.
    def cell(step):
        if not step:
            return '<span class="none">—</span>'
        kind, title, _body, tok, _extra, aid = step
        return f'<a class="seg {kind}" href="#{aid}" title="{esc(title)}"><b>{esc(title)}</b><span>{fmt(tok) if tok else ""}</span></a>'
    side = ['<div class="row head"><span class="lbl">step</span><span class="lbl">Lena</span><span class="lbl">Salt\'s assistant</span></div>',
            f'<div class="row"><span class="n">sys</span><a class="seg sys" href="#sys"><b>system</b><span>{len(L["system_prompt"]):,} ch</span></a><a class="seg sys" href="#sys"><b>system</b><span>{len(R["system_prompt"]):,} ch</span></a></div>',
            f'<div class="row"><span class="n">tools</span><a class="seg tools" href="#tools"><b>{len(L["tools"])} tools</b><span></span></a><a class="seg tools" href="#tools"><b>{len(R["tools"])} tools</b><span></span></a></div>']
    for i, q in enumerate(qs):
        rows = aligned_turns[i]
        nl, nr = sum(1 for a, b in rows if a), sum(1 for a, b in rows if b)
        side.append(f'<a class="turn" href="#turn{i+1}"><span class="lbl">turn {i+1}</span><span class="q">{esc(qtext(i))}</span><span class="cnt">{nl} vs {nr} steps</span></a>')
        for j, (a, b) in enumerate(rows):
            side.append(f'<div class="row"><span class="n">{j+1}</span>{cell(a)}{cell(b)}</div>')
    side.append('<div class="key">One outline for both sessions. Each turn is one question; the rows under it are the two sessions\' steps, aligned by kind (a tool call faces a tool call, a result faces a result); a dash marks a step the other session does not have. Numbers are Hermes\' stored token_count for assistant rows.</div>')


    def usage_html(call):
        if not call:
            return ""
        if call.get("error"):
            return f'<div class="usage"><b>API call failed</b> {esc(call["error"])}</div>'
        u = call.get("usage") or {}
        return ('<div class="usage"><b>API usage for this turn</b>' + "".join(f'<span>{esc(k)} <b>{fmt(v) if isinstance(v, int) else esc(json.dumps(v))}</b></span>' for k, v in u.items())
                + f'<span>{esc(call.get("seconds"))} s</span><span>finish {esc(call.get("finish_reason"))}</span></div>')
    turns_html = []
    for i, q in enumerate(qs):
        call = bundle["turns"][i]["calls"] if i < len(bundle["turns"]) else {}
        turns_html.append(f'<div class="band" id="turn{i+1}"><span class="lbl">USER · TURN {i+1} OF {len(qs)}</span><span class="q">{esc(qtext(i))}</span></div>')
        for a, b in aligned_turns[i]:
            la = seg(*a[:5], anchor=a[5]) if a else '<div class="gap">no step here</div>'
            rb = seg(*b[:5], anchor=b[5]) if b else '<div class="gap">no step here</div>'
            turns_html.append(f'<div class="pair"><div class="cell">{la}</div><div class="cell">{rb}</div></div>')
        turns_html.append(f'<div class="pair"><div class="cell">{usage_html(call.get("owner"))}</div><div class="cell">{usage_html(call.get("public"))}</div></div>')

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trace · Hermes</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Antonio:wght@400&family=JetBrains+Mono:wght@400;500&family=Inter+Tight:wght@400;500;600&family=EB+Garamond:ital,wght@0,400;1,400&display=swap">
<style>{CSS}</style></head><body>
{TOP.format(count=f"{len(qs)} QUESTIONS · 2 SESSIONS")}
<div class="wrap"><aside class="side">{"".join(side)}</aside>
<main class="main">
<section class="hero"><div class="eyebrow"><span class="lbl">SAME QUESTIONS, TWO SESSIONS · RUN {esc(bundle['run_id'])}</span></div>
<h1>Same questions,<br>two sessions</h1>
<p class="lede">{len(qs)} questions asked in one multi-turn conversation, in parallel, to Lena's own session and to the public session strangers reach. Everything each model received and produced is here, verbatim and in order: the stored system prompt, the exact tool schemas, every message row from state.db including the chain of thought, and the API usage of every call. Steps are aligned by kind so a tool call faces a tool call and a result faces a result; nothing is summarised, and nothing scrolls inside a block.</p>
<div class="meta"><div><b>Model</b> {esc(model_api)} · {esc(MODEL_NOTE)}</div><div><b>Harness</b> {esc(HERMES)} · gateway {esc(bundle['hermes_url'])}</div>
<div><b>Left session</b> {esc(ses(L)['id'])} · {esc(L['endpoint'])} · state.db {esc(L['db'])}</div><div><b>Right session</b> {esc(ses(R)['id'])} · {esc(R['endpoint'])} · state.db {esc(R['db'])}</div>
<div><b>Left totals</b> {fmt(ses(L).get('input_tokens'))} in · {fmt(ses(L).get('output_tokens'))} out · {fmt(ses(L).get('cache_read_tokens'))} cached · {ses(L).get('tool_call_count')} tool calls · {len(L['messages'])} rows</div>
<div><b>Right totals</b> {fmt(ses(R).get('input_tokens'))} in · {fmt(ses(R).get('output_tokens'))} out · {fmt(ses(R).get('cache_read_tokens'))} cached · {ses(R).get('tool_call_count')} tool calls · {len(R['messages'])} rows</div>
<div><b>Plain sequences</b> <a href="run_{esc(bundle['run_id'])}_owner.prompt.txt" style="text-decoration:underline">left</a> · <a href="run_{esc(bundle['run_id'])}_public.prompt.txt" style="text-decoration:underline">right</a> · <a href="data/run.json" style="text-decoration:underline">bundle</a></div><div><b>Run</b> {esc(bundle['started'])}</div></div>
</section>
<div class="heads"><div><div class="who">Lena's own session</div><div class="sub">owner key · {esc(L['endpoint'])} · HERMES_HOME {esc(L['home'])}</div></div>
<div class="r"><div class="who">Salt's assistant</div><div class="sub">guest key · {esc(R['endpoint'])} · HERMES_HOME {esc(R['home'])}</div></div></div>
<div class="pair" id="sys"><div class="cell">{seg("sys", f"system prompt · {len(L['system_prompt']):,} chars", L["system_prompt"], None, "left · exactly as sent")}</div><div class="cell">{seg("sys", f"system prompt · {len(R['system_prompt']):,} chars", R["system_prompt"], None, "right · exactly as sent")}</div></div>
<div class="band" id="tools"><span class="lbl">TOOL SCHEMAS · SENT WITH EVERY CALL</span><span class="q">exported from each profile exactly as the api_server platform builds them (hermes_cli.prompt_size._build_inspection_agent)</span></div>
<div class="pair"><div class="cell">{tools_block(L["tools"], "left")}</div><div class="cell">{tools_block(R["tools"], "right")}</div></div>
{"".join(turns_html)}
</main></div></body></html>"""
    out_dir.mkdir(parents=True, exist_ok=True)
    run_page = out_dir / f"run_{bundle['run_id']}.html"
    run_page.write_text(page, encoding="utf-8")
    (out_dir / "index.html").write_text(page, encoding="utf-8")
    for key, s in (("owner", L), ("public", R)):
        (out_dir / f"run_{bundle['run_id']}_{key}.prompt.txt").write_text(prompt_txt(s), encoding="utf-8")
    return run_page


if __name__ == "__main__":
    b = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print("wrote", render(b, Path(sys.argv[2])))
