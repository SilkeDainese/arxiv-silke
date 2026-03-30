"""Central weekly digest sender for AU student subscriptions."""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sys

from digest import (
    analyse_papers,
    apply_feedback_bias,
    detect_au_researchers,
    detect_delights,
    fetch_arxiv_papers,
    ingest_feedback_from_github,
    pre_filter,
    render_html,
    send_email,
    send_failure_report,
)
from setup.data import ASTRO_MINI_TRACKS, AU_STUDENT_TELESCOPE_KEYWORDS
from setup.student_presets import build_au_student_config
from student_registry import (
    AVAILABLE_STUDENT_PACKAGES,
    normalise_email,
    normalise_public_subscription,
    package_labels,
)

STUDENT_REGISTRY_URL = os.environ.get(
    "STUDENT_REGISTRY_URL",
    "https://arxiv-digest-relay.vercel.app/api/students",
).strip()
STUDENT_MANAGE_URL = os.environ.get("STUDENT_MANAGE_URL", STUDENT_REGISTRY_URL).strip()
STUDENT_TOKEN_SECRET = os.environ.get("STUDENT_TOKEN_SECRET", "").strip()
FEEDBACK_RELAY_URL = os.environ.get(
    "FEEDBACK_RELAY_URL",
    "https://arxiv-digest-relay.vercel.app/api/feedback",
).strip()

_SETTINGS_TOKEN_TTL = 7 * 86400  # 7 days


def _generate_settings_token(email: str, secret: str) -> str:
    """Create an HMAC-signed settings token (mirrors relay/_registry.py logic)."""
    data = {
        "email": email,
        "action": "change_settings",
        "payload": {},
        "expires_at": time.time() + _SETTINGS_TOKEN_TTL,
        "nonce": os.urandom(8).hex(),
    }
    data_bytes = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    data_b64 = base64.urlsafe_b64encode(data_bytes).decode("ascii")
    sig = hmac.new(secret.encode("utf-8"), data_bytes, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode("ascii")
    return f"{data_b64}.{sig_b64}"


def rewrite_summaries_for_students(
    papers: list[dict[str, Any]],
    api_key: str,
) -> None:
    """Rewrite plain_summary fields to be accessible to undergrad students.

    Uses a single batch API call to rewrite all summaries at once.
    Falls back gracefully — if anything fails, original summaries stay.
    """
    if not api_key or not papers:
        return

    titles_and_summaries = []
    for p in papers:
        titles_and_summaries.append({
            "title": p.get("title", ""),
            "summary": p.get("plain_summary", ""),
        })

    prompt = f"""Rewrite these astronomy paper summaries for 4th-semester university physics students taking an astronomy elective at Aarhus University.

Their physics background (they CAN handle real physics):
- Classical mechanics + advanced mechanics (Lagrangian, Hamiltonian)
- Electrodynamics, optics, special relativity
- Quantum mechanics + atomic physics
- Statistical physics, thermodynamics
- Linear algebra, calculus, differential equations
- Python programming and statistical data analysis
- Experimental lab methods

Their astronomy background (completed + current courses):
- Stars & Planets (completed): stellar evolution, HR diagrams, exoplanet detection (transits, radial velocity), photometry, spectroscopy basics, binary stars, stellar structure, nucleosynthesis in stars
- Galaxies & Cosmology (taking now): Milky Way structure, dark matter, supermassive black holes, elliptical/spiral galaxies, Tully-Fisher, galaxy clusters, gravitational lensing, Friedmann equation, expanding universe, cosmological parameters, CMB, Big Bang nucleosynthesis

Safe vocabulary (they know these — use freely): transit, radial velocity, spectroscopy, photometry, binary star, stellar evolution, main sequence, HR diagram, red giant, supernova, white dwarf, neutron star, black hole, dark matter, gravitational lensing, CMB, redshift, galaxy types, Milky Way, Tully-Fisher, nucleosynthesis.

Jargon to replace (use the plain version):
- asteroseismology → "star-interior measurements from oscillations"
- metallicity / [Fe/H] → "metal content"
- Rossby number → "rotation-activity ratio"
- isochrone → "age track"
- RGB/AGB/HB → spell out: "red giant branch" etc.
- secondary eclipse → "planet passing behind the star"
- phase curve → "brightness over a full orbit"
- atmospheric retrieval → "inferring atmosphere composition from spectra"
- Rossiter-McLaughlin / obliquity → "orbit-spin tilt"
- photoevaporation → "atmosphere stripped by radiation"
- Eddington luminosity → "maximum brightness from radiation pressure"
- magnetar → "neutron star with extreme magnetic field"
- kilonova → "flash from a neutron star merger"
- FRB → "millisecond radio burst"
- AGN feedback → "energy from black holes regulating star formation"
- SFR → "star formation rate"
- BAO → "sound-wave imprint in galaxy clustering"
- photo-z → "colour-based distance estimate"
- PSF → "how a point source blurs in an image"
- SNR / S/N → "signal-to-noise"
- MCMC → "parameter exploration method"
- Bayesian posterior → "updated probability after fitting"
- selection bias → "only detecting the brightest objects"
- Any named survey (SDSS, DES, LSST, Euclid, TESS, Kepler) → describe briefly
- "we constrain" → "we measured" or "we set a limit on"
- "consistent with" → "agrees with"
- "in tension with" → "disagrees with"
- "archival data" → "existing public observations"
- If you cannot simplify a term within 25 words, describe the result more broadly instead of keeping the jargon.

Rules:
- One sentence each, max 25 words
- Say what they FOUND, not how they did it — skip methods, pipelines, calibration
- No jargon, no acronyms, no parenthetical definitions like "point-spread-function (PSF)"
- If a term needs explaining, you used the wrong term — pick a simpler one
- No LaTeX, no symbols like $M_\\odot$ — write "solar mass" instead
- No hedging ("struggles to", "carefully calibrated") — just state the result
- Write like a smart friend explaining over coffee, not like an abstract
- Never start a sentence with "Researchers", "Scientists", "The authors", "A team", "The study", or "We present" — lead with what was found or what is new
- NEVER repeat the same phrasing or sentence structure across papers — vary your openings and word choices. If you notice yourself writing a similar pattern twice, rephrase.
- NEVER reproduce paper titles or author names — only rewrite the summary
- If the abstract describes a survey, mission, or tool: say what it measured or found, not what it is designed to do

Papers:
{json.dumps(titles_and_summaries, indent=2)}

Respond with ONLY a JSON array of objects, one per paper, in order:
[{{"summary": "..."}}, {{"summary": "..."}}]"""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        # Strip markdown fences if Claude wraps with ```json ... ```
        if text.startswith("```"):
            text = re.sub(r"^```[a-z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text)
        rewrites = json.loads(text)

        if len(rewrites) != len(papers):
            print(f"  ⚠️  Summary rewrite returned {len(rewrites)} items for {len(papers)} papers — skipping")
            return

        for paper, rewrite in zip(papers, rewrites):
            new_summary = rewrite.get("summary", "").strip()
            if new_summary:
                paper["plain_summary"] = new_summary

        print(f"  ✅ Rewrote {len(papers)} summaries for students")

    except Exception as e:
        print(f"  ⚠️  Student summary rewrite failed ({e}) — using originals")


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
    subscriptions: list[dict[str, Any]] = []
    for raw_subscription in data.get("subscriptions", []):
        try:
            subscriptions.append(normalise_public_subscription(raw_subscription))
        except (TypeError, ValueError) as exc:
            print(f"   ↷ Skipping invalid student subscription record: {exc}")
    return subscriptions


def _mark_welcome_sent(email: str) -> None:
    """Tell the relay to set welcome_sent=True for this student (best-effort)."""
    admin_token = os.environ.get("STUDENT_ADMIN_TOKEN", "").strip()
    if not admin_token:
        return
    payload = json.dumps({
        "action": "mark_welcome_sent",
        "admin_token": admin_token,
        "email": email,
    }).encode("utf-8")
    try:
        request = urllib.request.Request(
            STUDENT_REGISTRY_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
        print(f"   ✓ Marked welcome_sent for {email}")
    except Exception as exc:
        print(f"   ⚠️  Could not mark welcome_sent for {email}: {exc}")


def fetch_aggregate_feedback() -> dict[str, dict[str, Any]]:
    """Fetch aggregate expert votes from the central feedback store.

    Returns a dict mapping paper_id -> {up, down, net, keywords, ...}.
    Returns empty dict on error or when admin token is not set.
    """
    admin_token = os.environ.get("STUDENT_ADMIN_TOKEN", "").strip()
    if not admin_token:
        return {}

    payload = json.dumps(
        {"action": "aggregate", "admin_token": admin_token}
    ).encode("utf-8")
    try:
        request = urllib.request.Request(
            FEEDBACK_RELAY_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data.get("aggregated", {})
    except Exception as exc:
        print(f"   ⚠️  Could not fetch aggregate feedback: {exc}")
        return {}


def apply_aggregate_expert_signal(
    papers: list[dict[str, Any]], aggregated: dict[str, dict[str, Any]]
) -> None:
    """Annotate papers with aggregate expert up/down signal.

    Sets paper["expert_net"] from direct paper_id matches, plus
    keyword-level signal from keyword_signal:* entries.
    """
    if not aggregated:
        return

    # Build a keyword-level signal map from keyword_signal:* entries
    keyword_signal: dict[str, int] = {}
    for key, agg in aggregated.items():
        if key.startswith("keyword_signal:"):
            kw = key.removeprefix("keyword_signal:")
            keyword_signal[kw] = agg.get("net", 0)

    for paper in papers:
        # Direct paper match
        direct = aggregated.get(paper.get("id", ""), {})
        net = direct.get("net", 0)

        # Add keyword-level signal from opted-in researchers
        matched = paper.get("matched_keywords") or []
        for kw in matched:
            net += keyword_signal.get(kw.lower(), 0)

        paper["expert_net"] = net


def _freshness_score(paper: dict[str, Any]) -> float:
    """Return a 0-1 freshness score based on published date (1.0 = today)."""
    published = paper.get("published", "")
    if not published:
        return 0.0
    try:
        pub_date = datetime.fromisoformat(published.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - pub_date).total_seconds() / 86400
        return max(0.0, 1.0 - age_days / 7.0)
    except (ValueError, TypeError):
        return 0.0


_PRESTIGE_JOURNALS = [
    # Order matters: check specific names before broad ones
    ("nature astronomy", "Nature Astronomy"),
    ("nature physics", "Nature Physics"),
    ("nature communications", "Nature Comms"),
    ("physical review letters", "PRL"),
    ("annual review", "Annual Reviews"),
    ("astronomy & astrophysics", "A&A"),
    ("astrophysical journal letters", "ApJL"),
    ("monthly notices", "MNRAS"),
    # Broad patterns last — anchored to avoid "computer science", "natural" etc.
    ("nature,", "Nature"),
    ("nature ", "Nature"),
    ("science,", "Science"),
    ("science ", "Science"),
]


def detect_prestige(papers: list[dict[str, Any]]) -> None:
    """Annotate papers published in high-impact journals with a prestige flag."""
    for paper in papers:
        journal = paper.get("journal_ref", "").lower()
        if not journal:
            continue
        for pattern, label in _PRESTIGE_JOURNALS:
            if pattern in journal:
                paper["prestige_journal"] = label
                break


def _is_astronomy_relevant(paper: dict[str, Any]) -> bool:
    """Guard against non-astronomy papers leaking into the student digest.

    Papers from astro-ph.* always pass. Papers from other categories
    (e.g. stat.ML, cs.LG) must match at least one astronomy-specific
    track (not just methods_ml) or have AU colleague/author matches.
    This prevents pure ML/stats papers from appearing in the digest
    when the AI scorer is unavailable and keyword-only fallback is used.
    """
    if paper.get("category", "").startswith("astro-ph."):
        return True
    # Non-astronomy category: require evidence of real astronomy relevance
    astro_packages = set(paper.get("student_package_ids", [])) - {"methods_ml"}
    if astro_packages:
        return True
    if paper.get("colleague_matches") or paper.get("known_authors"):
        return True
    return False


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
        # Category matches are more specific than keyword-only matches. Sort so
        # that the package whose arXiv category covers this paper comes first,
        # ensuring the display badge reflects the paper's actual field.
        paper_cat = paper.get("category", "")
        matched_packages.sort(key=lambda tid: 0 if paper_cat in track_categories[tid] else 1)
        paper["student_package_ids"] = matched_packages
        paper["student_au_priority"] = int(
            bool(paper.get("colleague_matches"))
            or bool(matched_keywords.intersection(au_keyword_set))
        )


_MAX_METHODS_ML_PAPERS = 2  # Hard cap: never more than 2 methods/ML papers per digest
_CORE_ASTRO_TRACKS = {"stars", "exoplanets", "galaxies", "cosmology", "high_energy", "solar_helio", "instrumentation"}


def _is_ml_only_paper(paper: dict[str, Any]) -> bool:
    """True if a paper is only relevant via methods_ml, not core astronomy."""
    packages = set(paper.get("student_package_ids", []))
    if not packages or packages == {"methods_ml"}:
        return not paper.get("category", "").startswith("astro-ph.")
    return False


def select_student_papers(
    papers: list[dict[str, Any]], package_ids: list[str], max_papers_per_week: int
) -> list[dict[str, Any]]:
    """Return the ranked top papers for a student subscription.

    ALL astro-ph.* papers are always eligible — this is an astronomy digest.
    The student's chosen packages boost matching papers higher in the ranking
    but never exclude unmatched astronomy papers. Non-astro-ph papers (e.g.
    stat.ML) are only included if they match a selected package.

    Ranking priority (highest first):
      1. AU colleague/telescope papers (always top)
      2. Prestige journal (Nature, Science, etc.)
      3. Core astronomy category match
      4. Methods-only penalty
      5. Student's chosen package overlap (boost, not filter)
      6. AI relevance score
      7. Expert signal + freshness

    Methods/ML papers are hard-capped at 2 per digest.
    """
    wanted = set(package_ids)

    # All astro-ph papers are always candidates; non-astro-ph need a package match
    selected = [
        paper
        for paper in papers
        if paper.get("category", "").startswith("astro-ph.")
        or set(paper.get("student_package_ids", [])).intersection(wanted)
    ]

    def _sort_key(paper: dict[str, Any]) -> tuple:
        packages = set(paper.get("student_package_ids", []))
        is_core_astro = int(paper.get("category", "").startswith("astro-ph."))
        ml_only = int(_is_ml_only_paper(paper))
        has_core_track = int(bool(packages.intersection(_CORE_ASTRO_TRACKS)))
        has_prestige = int(bool(paper.get("prestige_journal")))
        au_priority = paper.get("student_au_priority", 0)
        package_overlap = len(packages.intersection(wanted))
        return (
            au_priority,                                                        # AU papers always first
            has_prestige,                                                       # prestige journals next
            is_core_astro + has_core_track,                                     # core astronomy topics
            -ml_only,                                                           # penalise methods-only papers
            package_overlap,                                                    # student's chosen tracks boost
            paper.get("relevance_score", 0),                                    # AI quality
            paper.get("expert_net", 0),                                         # aggregate expert signal
            _freshness_score(paper),                                            # freshness
        )

    selected.sort(key=_sort_key, reverse=True)

    # Apply hard cap on methods/ML papers
    capped: list[dict[str, Any]] = []
    ml_count = 0
    for paper in selected:
        if _is_ml_only_paper(paper):
            if ml_count >= _MAX_METHODS_ML_PAPERS:
                continue
            ml_count += 1
        capped.append(paper)
        if len(capped) >= max_papers_per_week:
            break
    return capped


def make_student_digest_config(base_config: dict[str, Any], subscription: dict[str, Any]) -> dict[str, Any]:
    """Return a per-student config used for rendering and sending."""
    config = copy.deepcopy(base_config)
    email = subscription["email"]
    config["recipient_email"] = email
    config["max_papers"] = int(subscription["max_papers_per_week"])
    # Token-authenticated settings URL — proves identity from the inbox.
    # Falls back to plain email URL if STUDENT_TOKEN_SECRET is not set.
    if STUDENT_TOKEN_SECRET:
        settings_token = _generate_settings_token(email, STUDENT_TOKEN_SECRET)
        settings_params = {"action": "settings", "token": settings_token}
        config["subscription_manage_url"] = (
            f"{STUDENT_MANAGE_URL}?{urllib.parse.urlencode(settings_params)}"
        )
    else:
        manage_params = {"email": email}
        config["subscription_manage_url"] = (
            f"{STUDENT_MANAGE_URL}?{urllib.parse.urlencode(manage_params)}"
        )
    unsub_params = {"email": email, "mode": "unsubscribe"}
    config["subscription_unsubscribe_url"] = (
        f"{STUDENT_MANAGE_URL}?{urllib.parse.urlencode(unsub_params)}"
    )
    labels = [package_labels()[package_id] for package_id in subscription["package_ids"]]
    config["tagline"] = "Your categories: " + ", ".join(labels)
    # First digest gets a welcome header; subsequent ones do not
    if not subscription.get("welcome_sent", True):
        config["show_welcome"] = True
    return config


def _preview_filename(email: str) -> str:
    """Return a filesystem-safe preview filename for a student email."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", normalise_email(email)).strip("._")
    return f"{safe or 'student'}.html"


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for student batch runs."""
    parser = argparse.ArgumentParser(description=__doc__)
    preview_group = parser.add_mutually_exclusive_group()
    preview_group.add_argument("--preview", action="store_true", help="Render previews instead of sending email.")
    preview_group.add_argument("--send-preview", action="store_true", help="Send one preview digest to RECIPIENT_EMAIL.")
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
    try:
        subscriptions = fetch_student_subscriptions()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            print(f"\n❌ Student registry auth failed (HTTP {exc.code}). Check STUDENT_ADMIN_TOKEN.")
        else:
            print(f"\n❌ Student registry returned HTTP {exc.code}.")
        return 1
    except urllib.error.URLError as exc:
        print(f"\n❌ Could not reach student registry: {exc.reason}")
        return 1
    except RuntimeError as exc:
        print(f"\n❌ {exc}")
        return 1
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

    if not papers:
        print("\n⚠️  No papers fetched — all arXiv category requests failed or returned nothing.")
        print("   Skipping student digests to avoid sending empty emails. Check the errors above.")
        return 1

    print("\n👍 Ingesting quick-feedback votes...")
    feedback_stats = ingest_feedback_from_github(base_config)
    apply_feedback_bias(papers, feedback_stats)

    print("\n🔍 Pre-filtering shared AU student pool...")
    candidates = pre_filter(papers)

    print("\n🗳️  Fetching aggregate expert votes...")
    aggregated = fetch_aggregate_feedback()
    if aggregated:
        print(f"   {len(aggregated)} paper/keyword signals loaded")
    else:
        print("   No aggregate feedback available (will rank without expert signal)")

    print("\n🤖 Analysing shared AU student pool...")
    ranked_papers, scoring_method = analyse_papers(candidates, base_config)
    annotate_student_packages(ranked_papers)
    detect_au_researchers(ranked_papers)
    detect_delights(ranked_papers)
    detect_prestige(ranked_papers)
    prestige_count = sum(1 for p in ranked_papers if p.get("prestige_journal"))
    if prestige_count:
        print(f"   ⭐ {prestige_count} paper(s) from high-impact journals")
    apply_aggregate_expert_signal(ranked_papers, aggregated)

    # Guard: remove non-astronomy papers that only matched generic methods_ml keywords
    pre_guard = len(ranked_papers)
    ranked_papers = [p for p in ranked_papers if _is_astronomy_relevant(p)]
    removed = pre_guard - len(ranked_papers)
    if removed:
        print(f"   🛡️  Removed {removed} non-astronomy paper(s) (category guard)")

    # Rewrite summaries for student readability (uses Haiku — cheap + fast)
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if anthropic_key:
        print("\n📝 Rewriting summaries for students...")
        rewrite_summaries_for_students(ranked_papers, anthropic_key)

    print(f"   {len(ranked_papers)} papers available for student selection ({scoring_method})")

    # ─────── Send-preview: one email to RECIPIENT_EMAIL ──────
    if args.send_preview:
        recipient_email = os.environ.get("RECIPIENT_EMAIL", "").strip()
        if not recipient_email:
            print("\n❌ --send-preview requires RECIPIENT_EMAIL env var.")
            return 1

        # Try to find Silke's own subscription for realistic rendering
        preview_sub = None
        for sub in active_subscriptions:
            if normalise_email(sub.get("email", "")) == normalise_email(recipient_email):
                preview_sub = sub
                break
        if preview_sub is None:
            # Fall back to a default config covering all categories
            preview_sub = {
                "email": recipient_email,
                "package_ids": list(AVAILABLE_STUDENT_PACKAGES),
                "max_papers_per_week": 20,
            }

        selected = select_student_papers(
            ranked_papers,
            list(preview_sub["package_ids"]),
            int(preview_sub["max_papers_per_week"]),
        )
        if not selected:
            print("\n⚠️  No matching papers for preview — nothing to send.")
            return 0

        missing = sum(1 for p in selected if not p.get("plain_summary", "").strip())
        if missing:
            print(f"\n⚠️  {missing}/{len(selected)} papers have no summary (AI rewrite may have failed)")

        preview_config = make_student_digest_config(base_config, preview_sub)
        preview_config["recipient_email"] = recipient_email
        html = render_html(
            selected, [], preview_config, date_str,
            own_papers=[], scoring_method=scoring_method,
        )
        print(f"\n📧 Sending preview digest ({len(selected)} papers) to {recipient_email}")
        if send_email(html, len(selected), date_str, preview_config,
                      papers=selected, subject_prefix="[PREVIEW] "):
            print("✨ Preview sent.\n")
            return 0
        print("❌ Preview send failed.\n")
        return 1

    # ─────── Pre-send validation ──────────────────────────────
    print("\n🔒 Pre-send validation...")
    validation_errors: list[str] = []
    student_selections: dict[str, list[dict[str, Any]]] = {}

    # 1. Papers must exist
    if not ranked_papers:
        validation_errors.append("No papers available — refusing to send empty digests")

    # 2. Majority of papers must be from astro-ph (sanity check on the pool)
    astro_count = sum(1 for p in ranked_papers if p.get("category", "").startswith("astro-ph."))
    if ranked_papers and astro_count < len(ranked_papers) * 0.5:
        validation_errors.append(
            f"Only {astro_count}/{len(ranked_papers)} papers are astro-ph — "
            "pool may be contaminated with non-astronomy papers"
        )

    # 3. Every active student must get papers (cache results to avoid double computation)
    student_selections: dict[str, list[dict[str, Any]]] = {}
    students_with_no_papers: list[str] = []
    for sub in active_subscriptions:
        email = normalise_email(sub["email"])
        selected = select_student_papers(
            ranked_papers, list(sub["package_ids"]), int(sub["max_papers_per_week"])
        )
        student_selections[email] = selected
        if not selected:
            students_with_no_papers.append(sub["email"])
    if students_with_no_papers:
        validation_errors.append(
            f"{len(students_with_no_papers)} student(s) would get empty digests: "
            + ", ".join(students_with_no_papers[:5])
        )

    # 4. Every paper must have a non-empty summary
    papers_without_summary = [
        p["title"][:60] for p in ranked_papers
        if not p.get("plain_summary", "").strip()
    ]
    if papers_without_summary:
        validation_errors.append(
            f"{len(papers_without_summary)} paper(s) have no summary: "
            + ", ".join(papers_without_summary[:3])
        )

    # 5. Check for duplicate emails (would cause double-sending)
    seen_emails: set[str] = set()
    duplicates: list[str] = []
    for sub in active_subscriptions:
        email = normalise_email(sub["email"])
        if email in seen_emails:
            duplicates.append(email)
        seen_emails.add(email)
    if duplicates:
        validation_errors.append(f"Duplicate student emails: {', '.join(duplicates)}")

    if validation_errors:
        print("\n❌ Pre-send validation FAILED:")
        for err in validation_errors:
            print(f"   • {err}")
        print("\n   Aborting student digest send. Fix the issues above.")
        return 1

    print(f"   ✅ {len(ranked_papers)} astronomy papers, "
          f"{len(active_subscriptions)} unique students, no empty digests")

    processed_count = 0
    skipped_count = 0
    failed_recipients: list[str] = []
    for subscription in active_subscriptions:
        try:
            email = normalise_email(subscription["email"])
            selected = student_selections.get(email) if student_selections else None
            if selected is None:
                selected = select_student_papers(
                    ranked_papers,
                    list(subscription["package_ids"]),
                    int(subscription["max_papers_per_week"]),
                )
            if not selected:
                print(f"   ↷ No matching papers for {subscription['email']} — skipping")
                skipped_count += 1
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
                    failed_recipients.append(subscription["email"])
                    continue
                # Mark welcome as sent after first successful delivery
                if student_config.get("show_welcome"):
                    _mark_welcome_sent(subscription["email"])
            processed_count += 1
        except Exception as exc:
            print(f"   ❌ Unexpected error for {subscription['email']}: {exc}")
            failed_recipients.append(subscription["email"])
            continue

    if preview_dir is not None:
        print(f"\n✨ Wrote {processed_count} student preview(s), skipped {skipped_count}.\n")
        return 0

    print(f"\n✨ Sent {processed_count} student digest(s), skipped {skipped_count}.")
    if failed_recipients:
        print("❌ Failed recipients: " + ", ".join(failed_recipients))
        return 1
    print()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as _exc:
        import traceback
        _tb = traceback.format_exc()
        print(f"\n❌ Unhandled exception in student digest pipeline:\n{_tb}", file=sys.stderr)
        # Best-effort failure notification using base config recipient (admin email)
        try:
            _admin_config = build_student_base_config()
            _admin_config["recipient_email"] = os.environ.get("RECIPIENT_EMAIL", "").strip()
            send_failure_report(_admin_config if _admin_config["recipient_email"] else None, _tb)
        except Exception as _report_exc:
            print(f"⚠️  Could not send failure report: {_report_exc}", file=sys.stderr)
        raise SystemExit(1) from None
