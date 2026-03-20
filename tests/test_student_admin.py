from pathlib import Path

import scripts.student_admin as admin


def test_render_subscription_rows_and_stats():
    subscriptions = [
        {
            "email": "one@example.com",
            "active": True,
            "max_papers_per_week": 5,
            "package_ids": ["exoplanets", "stars"],
        },
        {
            "email": "two@example.com",
            "active": False,
            "max_papers_per_week": 3,
            "package_ids": ["stars"],
        },
    ]

    text = admin.render_subscription_rows(subscriptions)
    counts = admin.compute_package_counts(subscriptions)

    assert "one@example.com | active | 5/week" in text
    assert "two@example.com | inactive | 3/week" in text
    assert counts["stars"] == 2
    assert counts["exoplanets"] == 1


def test_write_csv_exports_expected_columns(tmp_path):
    output = tmp_path / "students.csv"
    subscriptions = [
        {
            "email": "one@example.com",
            "active": True,
            "max_papers_per_week": 5,
            "package_ids": ["exoplanets"],
            "created_at": "2026-03-15T00:00:00+00:00",
            "updated_at": "2026-03-15T01:00:00+00:00",
        }
    ]

    admin.write_csv(output, subscriptions)

    content = output.read_text(encoding="utf-8")
    assert "email,active,max_papers_per_week,package_ids,package_labels,created_at,updated_at" in content
    assert "one@example.com,True,5,exoplanets,Planets & exoplanets" in content


def test_main_list_command_uses_fetch(monkeypatch, capsys):
    monkeypatch.setattr(admin, "resolve_admin_token", lambda explicit: "admin-token")
    monkeypatch.setattr(
        admin,
        "fetch_subscriptions",
        lambda registry_url, admin_token, include_inactive=False: [
            {
                "email": "student@example.com",
                "active": True,
                "max_papers_per_week": 4,
                "package_ids": ["galaxies"],
            }
        ],
    )

    exit_code = admin.main(["list"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "student@example.com | active | 4/week" in output
