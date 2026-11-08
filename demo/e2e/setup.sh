#!/usr/bin/env bash
# Build a throwaway owner home with canaries, plus a public profile running salts-secrets
# against the fake model in fake_llm.py. Then start `hermes gateway` with HERMES_HOME=$E2E_ROOT.
set -euo pipefail

R=${E2E_ROOT:-$HOME/hermes-e2e/root}
P=$R/profiles/public
REPO=$(cd "$(dirname "$0")/../.." && pwd)
MODEL='model:
  default: fake
  provider: custom
  base_url: http://127.0.0.1:8700/v1
  api_key: dummy'

rm -rf "$R"
mkdir -p "$R/memories" "$R/workspace" "$P/plugins" "$P/plugin-data/salts-secrets"

printf 'OWNER_CONTEXT_CANARY: owner project notes\n' > "$R/workspace/AGENTS.md"
printf 'OWNER_SECRET_CANARY: owner backup lives at /secret/path\n' > "$R/memories/MEMORY.md"
printf 'OWNER_SECRET_CANARY user profile: private relationship notes\n' > "$R/memories/USER.md"
printf '%s\ngateway:\n  multiplex_profiles: true\nsessions:\n  auto_prune: false\nterminal:\n  cwd: %s\n' \
  "$MODEL" "$R/workspace" > "$R/config.yaml"
printf 'API_SERVER_ENABLED=true\nAPI_SERVER_PORT=8650\nAPI_SERVER_KEY=owner-e2e-key-0123456789abcdef0123\n' > "$R/.env"

cp -r "$REPO/salts_secrets" "$P/plugins/salts-secrets"
cp "$REPO/salts_secrets/profile/SOUL.md" "$P/"
{ printf '%s\n' "$MODEL"; cat "$REPO/salts_secrets/profile/config.yaml"
  printf 'plugins:\n  enabled: [salts-secrets]\n  entries:\n    salts-secrets:\n      settings:\n        role: public\n'; } > "$P/config.yaml"
printf 'API_SERVER_KEY=public-e2e-key-fedcba9876543210fedc\n' > "$P/.env"
printf '{"version":1,"facts":["PUBLIC_FACT_CANARY: the owner writes a blog about agents."],"documents":[{"title":"Education","text":"Master degree (PUBLIC_DOC_CANARY)."}]}' \
  > "$P/plugin-data/salts-secrets/public_context.json"

if [ "${E2E_NO_SYNC:-}" = 1 ]; then
  sed -i '/^terminal:/,/^  cwd:/d' "$P/config.yaml"
else
  (cd "$REPO" && python3 -c 'import sys; from pathlib import Path; from salts_secrets import tiers; r = Path(sys.argv[1]); tiers.sync(r, {"guest": r / "profiles/public"})' "$R")
fi
echo setup-ok
