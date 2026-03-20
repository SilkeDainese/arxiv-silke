from pathlib import Path
import builtins
import os
import time

import scripts.friend_setup as friend_setup
import yaml
from scripts.friend_setup import pick_downloaded_config, prepare_config_text, rewrite_top_level_scalar


def test_pick_downloaded_config_chooses_latest_recent_yaml(tmp_path):
    started_at = time.time()
    old_file = tmp_path / "config-old.yaml"
    old_file.write_text("old: true\n")
    os.utime(old_file, (started_at - 30, started_at - 30))

    first = tmp_path / "config.yaml"
    first.write_text("first: true\n")
    os.utime(first, (started_at + 1, started_at + 1))

    latest = tmp_path / "config (1).yaml"
    latest.write_text("latest: true\n")
    os.utime(latest, (started_at + 2, started_at + 2))

    assert pick_downloaded_config(tmp_path, started_at) == latest


def test_rewrite_top_level_scalar_replaces_existing_value():
    text = 'digest_name: "Demo"\ngithub_repo: "wrong/repo"\n'

    rewritten = rewrite_top_level_scalar(text, "github_repo", "friend/arxiv-digest")

    assert 'github_repo: "friend/arxiv-digest"' in rewritten
    assert 'github_repo: "wrong/repo"' not in rewritten


def test_prepare_config_text_appends_missing_github_repo(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text('digest_name: "Demo"\nrecipient_email: "a@example.com"\n')

    prepared = prepare_config_text(config_path, "friend/arxiv-digest")

    assert prepared.endswith('github_repo: "friend/arxiv-digest"\n')
    assert 'recipient_email: "a@example.com"' in prepared


def test_collect_secret_values_for_relay(monkeypatch):
    answers = iter(["1", "friend@example.com"])
    secrets = iter(["relay-token", "", ""])

    monkeypatch.setattr(builtins, "input", lambda _: next(answers))
    monkeypatch.setattr(friend_setup.getpass, "getpass", lambda _: next(secrets))

    collected, mode = friend_setup.collect_secret_values()

    assert mode == "1"
    assert collected == {
        "RECIPIENT_EMAIL": "friend@example.com",
        "DIGEST_RELAY_TOKEN": "relay-token",
    }


def test_collect_secret_values_for_smtp(monkeypatch):
    answers = iter(["2", "friend@example.com", "friend@gmail.com"])
    secrets = iter(["app-password", "gemini-key", ""])

    monkeypatch.setattr(builtins, "input", lambda _: next(answers))
    monkeypatch.setattr(friend_setup.getpass, "getpass", lambda _: next(secrets))

    collected, mode = friend_setup.collect_secret_values()

    assert mode == "2"
    assert collected == {
        "RECIPIENT_EMAIL": "friend@example.com",
        "SMTP_USER": "friend@gmail.com",
        "SMTP_PASSWORD": "app-password",
        "GEMINI_API_KEY": "gemini-key",
    }


def test_collect_secret_values_allows_repo_only(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda _: "3")

    collected, mode = friend_setup.collect_secret_values()

    assert mode == "3"
    assert collected == {}


def test_collect_secret_values_can_skip_recipient_secret_when_config_has_it(monkeypatch):
    answers = iter(["1", ""])
    secrets = iter(["relay-token", "", ""])

    monkeypatch.setattr(builtins, "input", lambda _: next(answers))
    monkeypatch.setattr(friend_setup.getpass, "getpass", lambda _: next(secrets))

    collected, mode = friend_setup.collect_secret_values(
        recipient_email="student@example.com",
        recipient_in_config=True,
    )

    assert mode == "1"
    assert collected == {"DIGEST_RELAY_TOKEN": "relay-token"}


def test_build_au_student_terminal_config(monkeypatch):
    # Package order is now 8 items: stars, exoplanets, galaxies, cosmology,
    # high_energy, instrumentation, solar_helio, methods_ml.
    # Select exoplanets + galaxies → n, y, y, n, n, n, n, n.
    answers = iter(
        [
            "Student Example",
            "student@example.com",
            "n",  # stars
            "y",  # exoplanets
            "y",  # galaxies
            "n",  # cosmology
            "n",  # high_energy
            "n",  # instrumentation
            "n",  # solar_helio
            "n",  # methods_ml
            "2",
        ]
    )

    monkeypatch.setattr(builtins, "input", lambda _: next(answers))

    config_text, recipient_email = friend_setup.build_au_student_terminal_config()
    config = yaml.safe_load(config_text)

    assert recipient_email == "student@example.com"
    assert config["recipient_email"] == "student@example.com"
    assert config["student_tracks"] == [
        "AU Astronomy",
        "Planets & exoplanets",
        "Galaxies",
    ]
    assert config["max_papers"] == 4
    assert config["min_score"] == 6
