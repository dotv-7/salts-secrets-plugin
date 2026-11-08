"""Salt's Secrets: one Hermes, two minds.

The same plugin is installed in two profiles and plays a different part in each:

- owner profile: routes every message that is not the owner's own DM to the public profile, adds the
  ``hermes salts-secrets`` command and the Context / Permissions / Trace dashboard tabs, and refuses every
  tool in sessions opened with the demo's read-only passphrase, and lets the owner queue notes for guests
  from chat (``salt_propose_public``), which only an owner ``/salt approve`` publishes;
- public profile: refuses every tool outside the guest allowlist and injects only what the owner
  approved for strangers.
"""

from __future__ import annotations

import logging

from . import home

log = logging.getLogger(__name__)


def register(ctx) -> None:
    role = ctx.get_config("role", default="") or ""
    public_profile = ctx.get_config("public_profile", default="public") or "public"
    if role == "public" or (role != "owner" and home.is_public(home.current(), public_profile)):
        from . import gate, public_context, receipts

        gate.register(ctx)
        public_context.register(ctx)
        receipts.register(ctx)
        log.info("salts-secrets: public role")
        return
    from . import cli, notes, readonly, route

    route.register(ctx)
    readonly.register(ctx)
    notes.register(ctx)
    cli.register(ctx)
    log.info("salts-secrets: owner role")
