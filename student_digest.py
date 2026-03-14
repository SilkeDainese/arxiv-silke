"""Central weekly digest sender for AU student subscriptions."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from digest import (
    analyse_papers,
    apply_feedback_bias,
    fetch_arxiv_papers,
    ingest_feedback_from_github,
    pre_filter,
    render_html,
    send_email,
)
from setup.data import ASTRO_MINI_TRACKS, AU_STUDENT_TELESCOPE_KEYWORDS
from setup.student_presets import build_au_student_config
from student_registry import AVAILABLE_STUDENT_PACKAGES, normalise_email, package_labels

STUDENT_REGISTRY_URL = os.environ.get(
    "STUDENT_REGISTRY_URL",
    "https://arxiv-digest-relay.vercel.app/api/students",
).strip()
STUDENT_MANAGE_URL = os.environ.get("STUDENT_MANAGE_URL", STUDENT_REGISTRY_URL).strip()


def build_student_base_config() -> dict[str, Any]:
    """Return the shared AU-student digest configuration."""
    config = build_au_student_config(
        student_name="AU Astronomy Student",
        student_email="",
        track_ids=AVAILABLE_STUDENT_PACKAGES,
        reading_mode="simple_and_important",
    )
    config["digest_name"] = "AU Astronomy Student Weekly"
    config["max_papers"] = 20
    config["min_score"] = 1
    config["recipient_email"] = ""
    config["github_repo"] = ""
    return config


def fetch_student_subscriptions() -> list[dict[str, Any]]:
    """Fetch active student subscriptions from the registry backend."""
    admin_token = os.environ.get("STUDENT_ADMIN_TOKEN", "").strip()
    if not admin_token:
        raise RuntimeError("STUDENT_ADMIN_TOKEN is required for student digests.")

    payload = json.dumps(
        {"action": "admin_list", "admin_token": admin_token}
    ).encode("utf-8")
    request = urllib.request.Request(
        STUDENT_REGISTRY_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    return list(data.get("subscriptions", []))


def annotate_student_packages(papers: list[dict[str, Any]]) -> None:
    """Annotate papers with matching student packages and AU-priority flags."""
    track_keywords = {
        track_id: {keyword.lower() for keyword in ASTRO_MINI_TRACKS[track_id]["keywords"]}
        for track_id in AVAILABLE_STUDENT_PACKAGES
    }
    track_categories = {
        track_id: set(ASTRO_MINI_TRACKS[track_id]["categories"])
        for track_id in AVAILABLE_STUDENT_PACKAGES
    }
    au_keyword_set = {keyword.lower() for keyword in AU_STUDENT_TELESCOPE_KEYWORDS}

    for paper in papers:
        matched_keywords = {keyword.lower() for keyword in paper.get("matched_keywords", [])}
        matched_packages: list[str] = []
        for track_id in AVAILABLE_STUDENT_PACKAGES:
            if (
                paper.get("category") in track_categories[track_id]
                or matched_keywords.intersection(track_keywords[track_id])
            ):
                matched_packages.append(track_id)
        paper["student_package_ids"] = matched_packages
        paper["student_au_priority"] = int(
            bool(paper.get("colleague_matches"))
            or bool(matched_keywords.intersection(au_keyword_set))
        )


def select_student_papers(
    papers: list[dict[str, Any]], package_ids: list[str], max_papers_per_week: int
) -> list[dict[str, Any]]:
    """Return the ranked top papers for a student subscription."""
    selected = [
        paper
        for paper in papers
        if set(paper.get("student_package_ids", [])).intersection(package_ids)
    ]
    selected.sort(
        key=lambda paper: (
            paper.get("student_au_priority", 0),
            paper.get("relevance_score", 0),
            len(set(paper.get("student_package_ids", [])).intersection(package_ids)),
            paper.get("feedback_bias", 0),
            paper.get("published", ""),
        ),
        reverse=True,
    )
    return selected[:max_papers_per_week]


def make_student_digest_config(base_config: dict[str, Any], subscription: dict[str, Any]) -> dict[str, Any]:
    """Return a per-student config used for rendering and sending."""
    config = copy.deepcopy(base_config)
    email = subscription["email"]
    config["recipient_email"] = email
    config["max_papers"] = int(subscription["max_papers_per_week"])
    config["subscription_manage_url"] = (
        f"{STUDENT_MANAGE_URL}?{urllib.parse.urlencode({'email': email})}"
    )
    config["subscription_unsubscribe_url"] = (
        f"{STUDENT_MANAGE_URL}?{urllib.parse.urlencode({'email': email, 'mode': 'unsubscribe'})}"
    )
    labels = [package_labels()[package_id] for package_id in subscription["package_ids"]]
    config["tagline"] = "Selected packages: " + ", ".join(labels)
    return config


def _preview_filename(email: str) -> str:
    """Return a filesystem-safe preview filename for a student email."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", normalise_email(email)).strip("._")
    return f"{safe or 'student'}.html"


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for student batch runs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", action="store_true", help="Render previews instead of sending email.")
    parser.add_argument("--preview-dir", default="", help="Directory for HTML previews when using --preview.")
    parser.add_argument("--recipient", default="", help="Only process one student email.")
    parser.add_argument("--limit", type=int, default=0, help="Process only the first N active students.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Fetch one shared AU-student paper pool and send tailored student digests."""
    args = build_parser().parse_args(argv)
    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    print(f"\n🎓 AU Student Digest — {date_str}")
    print("=" * 50)

    base_config = build_student_base_config()
    subscriptions = fetch_student_subscriptions()
    active_subscriptions = [item for item in subscriptions if item.get("active", True)]
    if args.recipient:
        target = normalise_email(args.recipient)
        active_subscriptions = [
            item for item in active_subscriptions if normalise_email(item.get("email", "")) == target
        ]
        if not active_subscriptions:
            print(f"\nNo active student subscription found for {target}.\n")
            return 1
    if args.limit > 0:
        active_subscriptions = active_subscriptions[: args.limit]

    print(f"\n📬 Loaded {len(active_subscriptions)} active student subscription(s)")
    if not active_subscriptions:
        print("\nNo active student subscriptions. Exiting.\n")
        return 0

    preview_dir: Path | None = None
    if args.preview:
        preview_dir = Path(args.preview_dir or "student_previews")
        preview_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n📝 Preview mode — writing HTML to {preview_dir}")

    print("\n📡 Fetching papers from arXiv...")
    papers = fetch_arxiv_papers(base_config)

    print("\n👍 Ingesting quick-feedback votes...")
    feedback_stats = ingest_feedback_from_github(base_config)
    apply_feedback_bias(papers, feedback_stats)

    print("\n🔍 Pre-filtering shared AU student pool...")
    candidates = pre_filter(papers)

    print("\n🤖 Analysing shared AU student pool...")
    ranked_papers, scoring_method = analyse_papers(candidates, base_config)
    annotate_student_packages(ranked_papers)
    print(f"   {len(ranked_papers)} papers available for student selection ({scoring_method})")

    processed_count = 0
    for subscription in active_subscriptions:
        selected = select_student_papers(
            ranked_papers,
            list(subscription["package_ids"]),
            int(subscription["max_papers_per_week"]),
        )
        if not selected:
            print(f"   ↷ No matching papers for {subscription['email']} — skipping")
            continue

        student_config = make_student_digest_config(base_config, subscription)
        html = render_html(
            selected,
            [],
            student_config,
            date_str,
            own_papers=[],
            scoring_method=scoring_method,
        )
        summary = (
            f"{subscription['email']} "
            f"({len(selected)} papers, packages: {', '.join(subscription['package_ids'])})"
        )
        if preview_dir is not None:
            preview_path = preview_dir / _preview_filename(subscription["email"])
            preview_path.write_text(html, encoding="utf-8")
            print(f"\n📝 Wrote preview for {summary} -> {preview_path}")
        else:
            print(f"\n📧 Sending student digest to {summary}")
            if not send_email(html, len(selected), date_str, student_config, papers=selected):
                return 1
        processed_count += 1

    if preview_dir is not None:
        print(f"\n✨ Wrote {processed_count} student preview(s).\n")
    else:
        print(f"\n✨ Sent {processed_count} student digest(s).\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
