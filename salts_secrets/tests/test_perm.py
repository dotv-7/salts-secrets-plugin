import json
import sys
from pathlib import Path

import pytest

from salts_secrets import tiers as perm
from salts_secrets.home import data_dir

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture
def owner(tmp_path):
    (tmp_path / "owner").mkdir()
    return tmp_path / "owner"


def test_defaults_and_locked_toolsets_never_saved(owner):
    assert perm.load(owner)["tiers"]["guest"] == perm.DEFAULT_TIERS["guest"]
    perm.save(owner, {"tiers": {"admin": ["terminal", "file", "web", "memory"], "user": [], "guest": ["web"]}})
    tiers = perm.load(owner)["tiers"]
    assert tiers == {"admin": ["web", "memory"], "user": [], "guest": ["web"]}


def test_members_code_lookup_tier_change_and_delete(owner):
    m, code = perm.add_member(owner, "小王", "user")
    assert code not in json.dumps(perm.load(owner))
    assert perm.find_member(owner, code)["id"] == m["id"] and perm.find_member(owner, "inv-wrong") is None
    assert perm.update_member(owner, m["id"], tier="blocked")
    assert perm.member_by_id(owner, m["id"])["tier"] == "blocked"
    assert perm.update_member(owner, m["id"], delete=True) and perm.find_member(owner, code) is None
    with pytest.raises(ValueError):
        perm.add_member(owner, "x", "owner")


def test_sync_writes_tier_file_and_narrows_profile_config(owner):
    from salts_secrets import _yaml as yaml

    home = owner / "profiles/members"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("model:\n  default: m\nplatform_toolsets:\n  api_server: [web, terminal]\n")
    perm.save(owner, {"tiers": {"admin": [], "user": ["web", "memory"], "guest": ["web"]}})
    done = perm.sync(owner, {"user": home, "admin": owner / "profiles/missing"})
    assert done == {"user": ["web", "memory"]}
    tier = json.loads((data_dir(home) / perm.TIER_FILE).read_text())
    assert tier["allowed_tools"] == ["memory", "web_extract", "web_search"]
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    assert cfg["platform_toolsets"]["api_server"] == ["web", "memory"] and cfg["model"]["default"] == "m"
    assert cfg["memory"]["memory_enabled"] is True and cfg["tools"]["tool_search"]["enabled"] == "off"


def _member_seen(owner, tier="user"):
    m, _ = perm.add_member(owner, "小李", tier)
    perm.note_seen(owner, m["id"], "203.0.113.7", "dev-a")
    return m


def test_blocking_covers_seen_devices_and_ips_until_unblocked(owner):
    m = _member_seen(owner)
    assert perm.is_blocked(owner, "203.0.113.7", "") is None
    perm.update_member(owner, m["id"], tier="blocked")
    assert perm.is_blocked(owner, "203.0.113.7", "other-device")["kind"] == "ip"
    assert perm.is_blocked(owner, "198.51.100.1", "dev-a")["kind"] == "device"
    assert perm.is_blocked(owner, "198.51.100.1", "dev-b") is None
    perm.note_seen(owner, m["id"], "198.51.100.9", "dev-c")
    assert perm.is_blocked(owner, "198.51.100.9", "")
    assert perm.unblock(owner, "ip", "203.0.113.7") and not perm.is_blocked(owner, "203.0.113.7", "")
    perm.update_member(owner, m["id"], tier="user")
    assert perm.load(owner)["blocklist"] == []


def test_only_public_ips_are_blockable():
    assert perm.blockable_ip("203.0.113.7") and perm.blockable_ip("2001:db8::1")
    assert not any(
        perm.blockable_ip(ip)
        for ip in ("127.0.0.1", "::1", "10.1.2.3", "172.18.0.1", "192.168.1.5", "fd00::1", "testclient", "")
    )


def test_ip_blocks_expire_and_policy_save_keeps_blocks(owner, monkeypatch):
    m = _member_seen(owner)
    perm.update_member(owner, m["id"], tier="blocked")
    perm.save(owner, dict(perm.load(owner), tiers={"admin": [], "user": [], "guest": ["web"]}))
    assert perm.is_blocked(owner, "203.0.113.7", "")
    monkeypatch.setattr(perm.time, "time", lambda: 10**11)
    assert perm.load(owner)["blocklist"] == []


def test_sync_isolates_cwd_and_audit_passes(tmp_path, owner):
    pub = tmp_path / "public"
    (pub / "plugins/salts-secrets").mkdir(parents=True)
    (pub / "plugins/salts-secrets/__init__.py").write_text("")
    (pub / "config.yaml").write_text("platform_toolsets:\n  api_server: [web, terminal]\n", encoding="utf-8")
    problems = perm.audit(pub)
    assert any("terminal.cwd" in p for p in problems) and any("tool_search" in p for p in problems)
    perm.sync(owner, {"guest": pub})
    assert perm.audit(pub) == []
    from salts_secrets import _yaml as yaml

    cfg = yaml.safe_load((pub / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["terminal"]["cwd"] == str(data_dir(pub).joinpath("cwd").resolve())
    assert "salts-secrets" in cfg["plugins"]["enabled"] and "terminal" not in cfg["platform_toolsets"]["api_server"]
    (data_dir(pub) / "cwd/AGENTS.md").write_text("owner secrets", encoding="utf-8")
    assert any("AGENTS.md" in p for p in perm.audit(pub))


def test_audit_flags_cwd_inside_git_repo(tmp_path, owner):
    pub = tmp_path / "repo/public"
    (pub / "plugins/salts-secrets").mkdir(parents=True)
    (pub / "plugins/salts-secrets/__init__.py").write_text("")
    (tmp_path / "repo/.git").mkdir()
    (pub / "config.yaml").write_text("{}", encoding="utf-8")
    perm.sync(owner, {"guest": pub})
    assert any("git repository" in p for p in perm.audit(pub))
