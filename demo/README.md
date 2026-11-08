# Demo: Salt

Everything in this folder is the demo shown at https://salt.dotv.io/. None of it ships in the plugin.

Salt is a fictional anonymous crypto KOL (@saltwater_sats, 310k followers on X); her owner, Lena, is fiction too. Every fact,
password and address in the seed data is invented.

| Path | What |
|---|---|
| `SOUL.md` | Salt's public persona, used instead of the generic template in `salts_secrets/profile/` |
| `seed_demo.py` | Writes a demo owner home (memories, workspace, skills) to play with |
| `chat/server.py` | Web chat with a passphrase-unlocked owner mode, plus the original `/shelf`, `/perm`, `/trace` pages |
| `fake_hermes.py` | A stand-in for the Hermes API server, so the chat runs without a model |
| `trace_run.py`, `trace_render.py` | Ask the same questions to both profiles and render every byte each received |
| `e2e/` | Probe script against a real gateway with a fake LLM |

## Run it locally

```bash
pip install -r demo/requirements-dev.txt
python3 demo/seed_demo.py "$PWD/.demo/owner"
SALT_OWNER_HOME="$PWD/.demo/owner" SALT_OWNER_PASSPHRASE_HASH=... SALT_SECRET=<random> \
  python3 demo/chat/server.py          # http://127.0.0.1:8780/
```

`SALT_OWNER_HOME` defaults to `~/.hermes`; `SALT_PUBLIC_HOME` defaults to
`$SALT_OWNER_HOME/profiles/public`. Without `SALT_HERMES_URL` the chat talks to
`http://127.0.0.1:8642`, the Hermes API server (or `fake_hermes.py`).
`SALT_OWNER_NAME` and `SALT_PUBLIC_NAME` (default `Lena` and `Salt`) set the names the pages show.

### Read-only owner, dictation, Doc

- `SALT_VIEWER_PASSPHRASE_HASH` (same `hash_passphrase` format) adds a second passphrase for the same
  sign-in box. It opens the owner side read-only: chat with the owner's agent and browse Context,
  Permissions and Trace, with a `READ ONLY` badge. Every write route answers 403 `read-only mode`, and the
  chat runs under `salt-v-` session ids, which `salts_secrets/readonly.py` (owner profile, `pre_tool_call`)
  refuses every tool for. Each read-only session is capped at 12 turns of 2,000 characters, then a fresh
  one starts. `SALT_VIEWER_PASSPHRASE_SHOWN` prints that passphrase on `/doc` when you mean to publish it.
- The guest-version boxes on `/shelf` have a MIC button: the browser's own speech recognition (Chrome,
  Edge, Safari; Chrome sends audio to Google to transcribe) types into the box. Nothing is published
  until you click Save & sync. Browsers without speech recognition get no button.
- `/doc` (linked as DOC) is the developer reference for this install's HTTP API: copy-paste curl and
  Python calls against the site's own origin, the SSE format, sessions, read-only sign-in, error codes,
  and a SKILL.md another Hermes can install to talk to this one. The live part (`/api/doc`) is only
  counts, document titles, guest tools and the isolation line; no private item, path or key.

## Traces

`trace_render.py` replaces the owner home, the user's home directory and every loopback URL with
placeholders before writing anything under `chat/static/trace/`. Regenerate a trace after changing
the demo data instead of editing the files by hand.
