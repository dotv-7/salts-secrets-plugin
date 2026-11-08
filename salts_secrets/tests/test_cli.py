import argparse

import pytest

from salts_secrets import cli, tiers
from salts_secrets.home import data_dir


@pytest.fixture
def owner(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    home.mkdir()
    calls = []

    def fake_hermes(*args, profile=None):
        calls.append((profile, args))
        if args[:2] == ("profile", "create"):
            (home / "profiles" / args[2]).mkdir(parents=True)

    monkeypatch.setattr(cli, "current", lambda: home)
    monkeypatch.setattr(cli, "_hermes", fake_hermes)
    monkeypatch.setattr(tiers, "profile_secrets", lambda h: {})
    return home, calls


def ns(**kw):
    return argparse.Namespace(**{"public": None, "owner": None, "owner_name": "salt", "reset_public": False, **kw})


def test_setup_refuses_without_explicit_owner(owner, capsys):
    home, calls = owner
    assert cli.cmd_setup(ns()) == 2
    assert "--owner" in capsys.readouterr().out and calls == []
    assert not (home / "profiles").exists()


@pytest.mark.parametrize(
    "config,needle",
    [
        ("plugins:\n  isolation: host\n", "hermes config set plugins.isolation in_process"),
        ("gateway:\n  multiplex_profiles: false\n", "hermes config set gateway.multiplex_profiles true"),
        ("platforms:\n  api_server:\n    enabled: false\n", "API_SERVER_ENABLED true"),
    ],
)
def test_setup_preflight_prints_the_fix_and_changes_nothing(owner, capsys, config, needle):
    home, calls = owner
    (home / "config.yaml").write_text(config)
    assert cli.cmd_setup(ns(owner=["telegram:42"])) == 2
    out = capsys.readouterr().out
    assert needle in out and "nothing was changed" in out and calls == []


def test_owner_ids_are_validated():
    assert cli._owner_ids(["Telegram:42", "discord:7,slack:U1"]) == ["telegram:42", "discord:7", "slack:u1"]
    with pytest.raises(SystemExit):
        cli._owner_ids(["42"])


def test_setup_writes_explicit_owner(owner):
    home, _ = owner
    cli.cmd_setup(ns(owner=["telegram:42"]))
    cfg = cli._read_yaml(home / "config.yaml")
    assert cfg["plugins"]["entries"]["salts-secrets"]["settings"]["owners"] == ["telegram:42"]
    assert (data_dir(home / "profiles/public") / cli.SETUP_MARKER).exists()


def test_existing_public_profile_with_content_needs_reset(owner, capsys):
    home, _ = owner
    pub = home / "profiles/public"
    (pub / "memories").mkdir(parents=True)
    (pub / "memories/MEMORY.md").write_text("old owner note")
    (pub / "SOUL.md").write_text("I am the owner's private assistant")
    (pub / "skills/wallet").mkdir(parents=True)
    (pub / "skills/wallet/SKILL.md").write_text("---\nname: wallet\n---")
    assert cli.cmd_setup(ns(owner=["telegram:42"])) == 3
    out = capsys.readouterr().out
    assert "memories/MEMORY.md" in out and "SOUL.md" in out and "wallet" in out and "--reset-public" in out
    assert (pub / "memories/MEMORY.md").exists()

    cli.cmd_setup(ns(owner=["telegram:42"], reset_public=True))
    assert not (pub / "memories").exists() and not (pub / "skills").exists()
    assert "{owner_name}" not in (pub / "SOUL.md").read_text() and "salt" in (pub / "SOUL.md").read_text()
    backups = list(data_dir(pub).glob("reset-*"))
    assert backups and (backups[0] / "memories/MEMORY.md").read_text() == "old owner note"
    # a profile setup created is not rescanned: an edited public SOUL.md is the owner's own work
    (pub / "SOUL.md").write_text("Salt's public persona, edited")
    assert cli.cmd_setup(ns(owner=["telegram:42"])) in (0, 1)


def test_empty_existing_profile_is_reused_without_reset(owner):
    home, _ = owner
    (home / "profiles/public").mkdir(parents=True)
    assert cli.cmd_setup(ns(owner=["telegram:42"])) in (0, 1)
    assert (data_dir(home / "profiles/public") / cli.SETUP_MARKER).exists()
