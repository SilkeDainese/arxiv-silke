"""
arXiv Digest — Personalised paper curation engine.
Fetches new arXiv papers, scores them with AI (Claude → keyword fallback),
and sends a beautiful HTML digest via email.

Configuration lives in config.yaml — edit that file to update keywords, colleagues, etc.
Use the setup wizard to generate your config.

Created by Silke S. Dainese · dainese@phys.au.dk · silkedainese.github.io
"""
from __future__ import annotations

import copy
import html as html_mod
import os
import json
import re
import smtplib
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import yaml

DEFAULT_SETUP_URL = "https://arxiv-digest-production-93ba.up.railway.app"

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

try:
    from google import genai as google_genai
    HAS_VERTEX_GEMINI = True
except ImportError:
    HAS_VERTEX_GEMINI = False

# ─────────────────────────────────────────────────────────────
#  CONFIGURATION (loaded from config.yaml)
# ─────────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "config.yaml"
CONFIG_EXAMPLE_PATH = Path(__file__).parent / "config.example.yaml"
STATS_PATH = Path(__file__).parent / "keyword_stats.json"
FEEDBACK_STATS_PATH = Path(__file__).parent / "feedback_stats.json"


def load_config() -> dict[str, Any]:
    """Load and validate configuration from config.yaml with sensible defaults."""
    # Use config.yaml if it exists, otherwise fall back to config.example.yaml
    if CONFIG_PATH.exists():
        config_file = CONFIG_PATH
    elif CONFIG_EXAMPLE_PATH.exists():
        print("  ⚠️  No config.yaml found — using config.example.yaml (upload your own config!)")
        config_file = CONFIG_EXAMPLE_PATH
    else:
        raise FileNotFoundError("No config.yaml or config.example.yaml found. Run the setup wizard to generate one.")

    with open(config_file) as f:
        cfg = yaml.safe_load(f)

    if not isinstance(cfg, dict):
        raise ValueError("config.yaml is empty or not a YAML mapping")

    # ── New fields with defaults ──
    cfg.setdefault("digest_name", "arXiv Digest")
    cfg.setdefault("researcher_name", "Reader")
    cfg.setdefault("research_context", "")
    cfg.setdefault("institution", "")
    cfg.setdefault("department", "")
    cfg.setdefault("tagline", "")
    cfg.setdefault("github_repo", "")
    cfg.setdefault("smtp_server", "smtp.gmail.com")
    cfg.setdefault("smtp_port", 587)
    try:
        cfg["smtp_port"] = int(cfg["smtp_port"])
    except (TypeError, ValueError):
        print(f"  ⚠️  Invalid smtp_port '{cfg['smtp_port']}' — defaulting to 587")
        cfg["smtp_port"] = 587
    cfg.setdefault("digest_mode", "highlights")  # "highlights" or "in_depth"
    cfg.setdefault("recipient_view_mode", "deep_read")  # "deep_read" or "5_min_skim"
    cfg.setdefault("self_match", [])  # patterns to match YOUR name in author lists
    cfg.setdefault("keyword_aliases", {})  # optional keyword -> [similar phrases]
    cfg.setdefault("own_api_key", False)  # set True when user adds their own AI key
    cfg.setdefault("allow_feedback_for_students", False)  # mirror votes to central store

    # ── Existing fields with defaults ──
    cfg.setdefault("categories", ["astro-ph.EP", "astro-ph.SR", "astro-ph.GA"])
    # If categories ended up empty/missing, default to broad physics
    if not cfg["categories"]:
        cfg["categories"] = ["physics"]
    cfg.setdefault("keywords", {})
    cfg.setdefault("research_authors", [])
    cfg.setdefault("colleagues", {"people": [], "institutions": []})
    cfg.setdefault("days_back", 3)

    # ── Digest mode defaults for min_score / max_papers ──
    # Only kicks in when user hasn't set them explicitly.
    mode = cfg["digest_mode"]
    if mode == "highlights":
        cfg.setdefault("max_papers", 6)
        cfg.setdefault("min_score", 5)
    elif mode == "in_depth":
        cfg.setdefault("max_papers", 15)
        cfg.setdefault("min_score", 2)
    else:
        cfg.setdefault("max_papers", 8)
        cfg.setdefault("min_score", 3)

    # ── Backward compat: flat keyword list → weighted dict ──
    if isinstance(cfg["keywords"], str):
        raise ValueError("keywords must be a YAML mapping (keyword: weight), not a bare string")
    if isinstance(cfg["keywords"], list):
        cfg["keywords"] = {kw: 5 for kw in cfg["keywords"]}
    if not isinstance(cfg["keyword_aliases"], dict):
        cfg["keyword_aliases"] = {}
    else:
        normalised_aliases: dict[str, list[str]] = {}
        for key, aliases in cfg["keyword_aliases"].items():
            clean_key = str(key).strip()
            if not clean_key:
                continue
            if isinstance(aliases, str):
                alias_list = [aliases]
            elif isinstance(aliases, list):
                alias_list = [str(alias).strip() for alias in aliases if str(alias).strip()]
            else:
                continue
            normalised_aliases[clean_key] = list(dict.fromkeys(alias_list))
        cfg["keyword_aliases"] = normalised_aliases

    # ── Backward compat: flat colleagues list → people/institutions ──
    if not isinstance(cfg.get("colleagues"), (dict, list)):
        cfg["colleagues"] = {}
    if isinstance(cfg["colleagues"], list):
        cfg["colleagues"] = {"people": cfg["colleagues"], "institutions": []}
    elif isinstance(cfg["colleagues"], dict):
        cfg["colleagues"].setdefault("people", [])
        cfg["colleagues"].setdefault("institutions", [])
    cfg["colleagues"]["people"] = _normalise_colleague_people(
        cfg["colleagues"].get("people", [])
    )

    # ── Environment overrides (env var wins, config.yaml as fallback) ──
    cfg["recipient_email"] = os.environ.get("RECIPIENT_EMAIL", "").strip() or cfg.get("recipient_email", "")
    cfg["github_repo"] = os.environ.get("GITHUB_REPOSITORY", "").strip() or cfg.get("github_repo", "")
    cfg["setup_url"] = os.environ.get("SETUP_WIZARD_URL", "").strip() or cfg.get("setup_url", DEFAULT_SETUP_URL)

    # Backward/typo-safe normalisation for recipient view mode
    mode = str(cfg.get("recipient_view_mode", "deep_read")).strip().lower().replace("-", "_")
    if mode in {"skim", "5min", "5_min", "5_minute_skim", "5_min_skim"}:
        cfg["recipient_view_mode"] = "5_min_skim"
    else:
        cfg["recipient_view_mode"] = "deep_read"
    return cfg


# ─────────────────────────────────────────────────────────────
#  KEYWORD TRACKING
# ─────────────────────────────────────────────────────────────

def load_keyword_stats() -> dict[str, Any]:
    """Load keyword hit statistics from disk, or return empty dict if none exist."""
    if STATS_PATH.exists():
        with open(STATS_PATH) as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                print("  ⚠️  keyword_stats.json is corrupted — resetting stats")
                return {}
    return {}


def save_keyword_stats(stats: dict[str, Any]) -> None:
    """Persist keyword hit statistics to disk as JSON (atomic write)."""
    tmp_path = STATS_PATH.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(stats, f, indent=2)
    os.replace(tmp_path, STATS_PATH)


def load_feedback_stats() -> dict[str, Any]:
    """Load feedback-derived keyword preferences from disk."""
    if FEEDBACK_STATS_PATH.exists():
        with open(FEEDBACK_STATS_PATH) as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                print("  ⚠️  feedback_stats.json is corrupted — resetting feedback stats")
                return {"processed_issue_ids": [], "keyword_feedback": {}, "updated_at": None}
    return {
        "processed_issue_ids": [],
        "keyword_feedback": {},
        "updated_at": None,
    }


def save_feedback_stats(stats: dict[str, Any]) -> None:
    """Persist feedback-derived keyword preferences to disk (atomic write)."""
    tmp_path = FEEDBACK_STATS_PATH.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(stats, f, indent=2)
    os.replace(tmp_path, FEEDBACK_STATS_PATH)


def _normalise_colleague_people(people: Any) -> list[dict[str, Any]]:
    """Normalise colleague entries to dicts with name/match/note fields."""
    normalised: list[dict[str, Any]] = []
    for person in people or []:
        if isinstance(person, str):
            clean_name = person.strip()
            if clean_name:
                normalised.append({"name": clean_name, "match": [clean_name]})
            continue
        if not isinstance(person, dict):
            continue
        name = str(person.get("name", "")).strip()
        matches = person.get("match", [])
        if isinstance(matches, str):
            match_list = [matches.strip()] if matches.strip() else []
        else:
            match_list = [
                str(match).strip()
                for match in matches
                if str(match).strip()
            ]
        if not match_list and name:
            match_list = [name]
        if not name and match_list:
            name = match_list[0]
        if not name:
            continue
        entry = {"name": name, "match": list(dict.fromkeys(match_list))}
        note = str(person.get("note", "")).strip()
        if note:
            entry["note"] = note
        normalised.append(entry)
    return normalised


def _keyword_token_forms(token: str) -> set[str]:
    """Return lightweight lexical variants for fuzzy keyword matching."""
    clean = re.sub(r"[^a-z0-9]+", "", token.lower())
    if not clean:
        return set()
    forms = {clean}
    if len(clean) >= 6 and clean.endswith("ies"):
        forms.add(clean[:-3] + "y")
    if len(clean) >= 6 and clean.endswith("ves"):
        forms.add(clean[:-3] + "f")
    if len(clean) >= 6 and clean.endswith("es"):
        forms.add(clean[:-2])
    if len(clean) >= 5 and clean.endswith("s"):
        forms.add(clean[:-1])
    if len(clean) >= 7 and clean.endswith("ary"):
        forms.add(clean[:-3])
    return {form for form in forms if len(form) >= 3}


def _tokenise_for_keyword_match(text: str) -> list[str]:
    """Tokenise free text into comparable word stems."""
    tokens: list[str] = []
    for raw in re.findall(r"[a-z0-9][a-z0-9-]+", text.lower()):
        tokens.extend(sorted(_keyword_token_forms(raw)))
    return list(dict.fromkeys(tokens))


def _tokens_match(keyword_token: str, paper_tokens: list[str]) -> bool:
    """Match a keyword token against paper tokens with prefix-aware fuzziness."""
    keyword_forms = _keyword_token_forms(keyword_token)
    if not keyword_forms:
        return False
    for candidate in paper_tokens:
        candidate_forms = _keyword_token_forms(candidate)
        for left in keyword_forms:
            for right in candidate_forms:
                if left == right:
                    return True
                shorter, longer = (
                    (left, right) if len(left) <= len(right) else (right, left)
                )
                if len(shorter) >= 5 and longer.startswith(shorter):
                    return True
    return False


def _keyword_aliases_for(keyword: str, config: dict[str, Any]) -> list[str]:
    """Return configured alias phrases for a keyword, matched case-insensitively."""
    aliases = [keyword]
    alias_map = config.get("keyword_aliases", {}) or {}
    keyword_lower = keyword.strip().lower()
    for raw_key, raw_aliases in alias_map.items():
        if raw_key.strip().lower() != keyword_lower:
            continue
        aliases.extend(str(alias).strip() for alias in raw_aliases if str(alias).strip())
    return list(dict.fromkeys(alias for alias in aliases if alias))


def _keyword_variant_matches(variant: str, text_lower: str, paper_tokens: list[str]) -> bool:
    """Match a keyword phrase using exact, token-set, and plural/hyphen tolerant logic."""
    clean_variant = " ".join(part for part in re.split(r"[^a-z0-9]+", variant.lower()) if part)
    if not clean_variant:
        return False
    if clean_variant in text_lower:
        return True
    variant_tokens = [token for token in clean_variant.split() if token]
    if not variant_tokens:
        return False
    return all(_tokens_match(token, paper_tokens) for token in variant_tokens)


def _matched_keywords_for_text(text: str, config: dict[str, Any]) -> list[str]:
    """Return canonical keywords matched by the paper text, including aliases."""
    text_lower = text.lower()
    paper_tokens = _tokenise_for_keyword_match(text)
    matched: list[str] = []
    for keyword in config.get("keywords", {}):
        variants = _keyword_aliases_for(keyword, config)
        if any(
            _keyword_variant_matches(variant, text_lower, paper_tokens)
            for variant in variants
        ):
            matched.append(keyword)
    return matched


def update_keyword_stats(papers: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    """Track which keywords matched papers in this run."""
    stats = load_keyword_stats()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for kw in config["keywords"]:
        if kw not in stats:
            stats[kw] = {"total_hits": 0, "last_hit": None, "runs_checked": 0}
        stats[kw]["runs_checked"] += 1

    for paper in papers:
        matched_keywords = _matched_keywords_for_text(
            paper["title"] + " " + paper["abstract"], config
        )
        for kw in matched_keywords:
            stats[kw]["total_hits"] += 1
            stats[kw]["last_hit"] = today

    save_keyword_stats(stats)

    # Report dormant keywords (no hits in 20+ runs)
    dormant = [kw for kw, s in stats.items()
               if s["runs_checked"] >= 20 and s["total_hits"] == 0]
    if dormant:
        print(f"  💤 Dormant keywords (0 hits in 20+ runs): {', '.join(dormant)}")

    return stats


def _parse_feedback_issue(issue: dict[str, Any]) -> tuple[str | None, list[str]]:
    """Extract (feedback_type, matched_keywords) from an issue body."""
    body = issue.get("body") or ""
    lines = [line.strip() for line in body.splitlines() if line.strip()]

    feedback_type = None
    keywords: list[str] = []
    for line in lines:
        low = line.lower()
        if low.startswith("feedback_type:"):
            feedback_type = line.split(":", 1)[1].strip().lower().replace("-", "_")
        elif low.startswith("matched_keywords:"):
            raw = line.split(":", 1)[1].strip()
            keywords = [kw.strip() for kw in raw.split(",") if kw.strip()]

    if feedback_type not in {"relevant", "not_relevant", "not relevant"}:
        return None, []
    if feedback_type == "not relevant":
        feedback_type = "not_relevant"
    return feedback_type, keywords


def _next_github_link(link_header: str) -> str | None:
    """Extract the next-page URL from a GitHub Link header."""
    for part in link_header.split(","):
        if 'rel="next"' not in part:
            continue
        match = re.search(r"<([^>]+)>", part)
        if match:
            return match.group(1)
    return None


def _fetch_github_feedback_issues(
    github_repo: str, token: str, per_page: int = 100, max_pages: int = 10
) -> list[dict[str, Any]]:
    """Fetch labeled feedback issues from GitHub with pagination."""
    issues: list[dict[str, Any]] = []
    next_url = (
        f"https://api.github.com/repos/{github_repo}/issues"
        f"?state=all&labels=digest-feedback&per_page={per_page}"
    )
    pages_fetched = 0

    while next_url and pages_fetched < max_pages:
        req = urllib.request.Request(next_url)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")

        with urllib.request.urlopen(req, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, list):
                issues.extend(payload)
            next_url = _next_github_link(response.headers.get("Link", ""))
        pages_fetched += 1

    return issues


def ingest_feedback_from_github(config: dict[str, Any]) -> dict[str, Any]:
    """Pull feedback issues from GitHub and update keyword preference stats.

    Expected issue body fields created by quick-feedback links:
      feedback_type: relevant|not_relevant
      matched_keywords: keyword1, keyword2
    """
    stats = load_feedback_stats()
    github_repo = config.get("github_repo", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()

    if not github_repo or not token:
        return stats

    processed: set[int] = set()
    for item in stats.get("processed_issue_ids", []):
        try:
            processed.add(int(item))
        except (TypeError, ValueError):
            continue

    try:
        issues = _fetch_github_feedback_issues(github_repo, token)
    except Exception as e:
        print(f"  ⚠️  Could not ingest feedback issues: {e}")
        return stats

    keyword_feedback = stats.get("keyword_feedback", {})
    new_count = 0

    for issue in issues:
        if "pull_request" in issue:
            continue
        issue_id = issue.get("id")
        if not issue_id or issue_id in processed:
            continue

        feedback_type, keywords = _parse_feedback_issue(issue)
        processed.add(issue_id)
        if not feedback_type or not keywords:
            continue

        delta = 1 if feedback_type == "relevant" else -1
        for kw in keywords:
            key = kw.lower()
            current = int(keyword_feedback.get(key, 0))
            keyword_feedback[key] = max(-5, min(5, current + delta))
        new_count += 1

    stats["processed_issue_ids"] = sorted(processed)
    stats["keyword_feedback"] = keyword_feedback
    stats["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    save_feedback_stats(stats)

    if new_count:
        print(f"  👍 Applied {new_count} new feedback vote(s) from GitHub issues")
    return stats


def apply_feedback_bias(papers: list[dict[str, Any]], feedback_stats: dict[str, Any]) -> None:
    """Apply keyword-level feedback preferences to paper ranking features in-place."""
    pref = feedback_stats.get("keyword_feedback", {}) if feedback_stats else {}
    if not pref:
        return

    for paper in papers:
        matched = paper.get("matched_keywords") or []
        bias = sum(int(pref.get(kw.lower(), 0)) for kw in matched)
        paper["feedback_bias"] = bias


def mirror_feedback_to_central(
    feedback_stats: dict[str, Any], config: dict[str, Any]
) -> int:
    """Mirror opted-in researcher votes to the central feedback store.

    Only runs when config has allow_feedback_for_students: true.
    Sends anonymised keyword-level votes — no researcher identity is transmitted.
    Returns the number of votes accepted by the central store, or 0 on skip/error.
    """
    if not config.get("allow_feedback_for_students"):
        return 0

    relay_url = os.environ.get(
        "FEEDBACK_RELAY_URL",
        "https://arxiv-digest-relay.vercel.app/api/feedback",
    ).strip()
    relay_token = os.environ.get("FEEDBACK_RELAY_TOKEN", "").strip()
    if not relay_token:
        return 0

    keyword_feedback = feedback_stats.get("keyword_feedback", {})
    if not keyword_feedback:
        return 0

    # Build anonymised votes from keyword-level preference deltas.
    # Each keyword with a non-zero bias becomes an up or down vote
    # attributed to a synthetic "keyword" paper_id so the central
    # store can aggregate the signal without knowing individual papers.
    votes: list[dict[str, Any]] = []
    categories = config.get("categories", [])
    package_tags = _categories_to_package_tags(categories)

    for keyword, bias in keyword_feedback.items():
        if bias == 0:
            continue
        votes.append({
            "paper_id": f"keyword_signal:{keyword}",
            "vote": "up" if bias > 0 else "down",
            "keywords": [keyword],
            "package_tags": package_tags,
        })

    if not votes:
        return 0

    payload = json.dumps({
        "action": "submit",
        "token": relay_token,
        "votes": votes,
    }).encode("utf-8")

    try:
        request = urllib.request.Request(
            relay_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
        accepted = data.get("accepted", 0)
        if accepted:
            print(f"  🔄 Mirrored {accepted} keyword vote(s) to central student store")
        return accepted
    except Exception as exc:
        print(f"  ⚠️  Could not mirror feedback to central store: {exc}")
        return 0


def _categories_to_package_tags(categories: list[str]) -> list[str]:
    """Map arXiv categories to broad student package tags."""
    tag_map = {
        "astro-ph.EP": "exoplanets",
        "astro-ph.SR": "stars",
        "astro-ph.GA": "galaxies",
        "astro-ph.CO": "cosmology",
    }
    tags: list[str] = []
    for cat in categories:
        tag = tag_map.get(cat)
        if tag and tag not in tags:
            tags.append(tag)
    return tags


# ─────────────────────────────────────────────────────────────
#  ARXIV FETCHING
# ─────────────────────────────────────────────────────────────

def fetch_arxiv_papers(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch recent papers from the arXiv API for configured categories.

    Args:
        config: Application configuration with categories, days_back, keywords, etc.

    Returns:
        Deduplicated list of paper dicts with metadata and normalised keyword scores.
    """
    papers = []
    for i, category in enumerate(config["categories"]):
        if i > 0:
            time.sleep(3)  # arXiv etiquette: pause between requests

        params = {
            "search_query": f"cat:{category}",
            "start": 0,
            "max_results": 100,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
        print(f"  Fetching {category}...")

        req = urllib.request.Request(url)
        req.add_header("User-Agent", "arxiv-digest/1.0 (GitHub Actions; https://github.com/SilkeDainese/arxiv-digest)")

        xml_data = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    xml_data = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:
                    wait = 10 * (attempt + 1)
                    print(f"  ⏳ Rate limited on {category}, retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                print(f"  ⚠️  Error fetching {category}: {e}")
            except Exception as e:
                print(f"  ⚠️  Error fetching {category}: {e}")
                break
        if xml_data is None:
            continue

        try:
            root = ET.fromstring(xml_data)
        except ET.ParseError as exc:
            print(f"  ⚠️  Failed to parse arXiv XML for {category}: {exc}")
            continue
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        cutoff = datetime.now(timezone.utc) - timedelta(days=config["days_back"])

        all_entries = root.findall("atom:entry", ns)
        skipped_malformed = 0
        for entry in all_entries:
            try:
                published_str = entry.find("atom:published", ns).text
                published = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
                if published < cutoff:
                    continue

                arxiv_id = entry.find("atom:id", ns).text.split("/abs/")[-1]
                title = (entry.find("atom:title", ns).text or "").strip().replace("\n", " ")
                abstract = (entry.find("atom:summary", ns).text or "").strip().replace("\n", " ")
                authors = [a.find("atom:name", ns).text for a in entry.findall("atom:author", ns) if a.find("atom:name", ns) is not None and a.find("atom:name", ns).text]
            except (AttributeError, TypeError, ValueError):
                skipped_malformed += 1
                continue

            # Use the paper's actual primary category, not the query category
            primary_cat_el = entry.find("{http://arxiv.org/schemas/atom}primary_category")
            paper_category = (
                primary_cat_el.get("term", category)
                if primary_cat_el is not None
                else category
            )

            # Check research authors (relevance boost)
            known_flag = []
            for author in authors:
                for known in config["research_authors"]:
                    if known.lower() in author.lower():
                        known_flag.append(author)
                        break

            # Check colleagues — people matches
            colleague_flag = []
            colleague_details: list[dict[str, str]] = []
            for author in authors:
                for colleague in config["colleagues"]["people"]:
                    for pattern in colleague.get("match", []):
                        if pattern.lower() in author.lower():
                            colleague_name = colleague.get("name", "Unknown")
                            if colleague_name not in colleague_flag:
                                colleague_flag.append(colleague_name)
                            detail = {"name": colleague_name}
                            note = str(colleague.get("note", "")).strip()
                            if note and not any(
                                existing.get("name") == colleague_name
                                for existing in colleague_details
                            ):
                                detail["note"] = note
                                colleague_details.append(detail)
                            elif not note and not any(
                                existing.get("name") == colleague_name
                                for existing in colleague_details
                            ):
                                colleague_details.append(detail)
                            break

            # Check colleagues — institutional matches (arXiv affiliation XML + abstract fallback)
            affiliations = []
            author_affiliations: dict[str, list[str]] = {}
            ns_arxiv = {"arxiv": "http://arxiv.org/schemas/atom"}
            for author_el in entry.findall("atom:author", ns):
                author_name = author_el.findtext("atom:name", "", ns)
                affs = [
                    aff_el.text
                    for aff_el in author_el.findall("arxiv:affiliation", ns_arxiv)
                    if aff_el.text
                ]
                if affs and author_name:
                    author_affiliations[author_name] = affs
                affiliations.extend(affs)
            affiliation_text = " ".join(affiliations).lower()
            text_lower = (title + " " + abstract).lower()
            for inst in config["colleagues"].get("institutions", []):
                inst_lower = inst.lower()
                if inst_lower in affiliation_text or inst_lower in text_lower:
                    if inst not in colleague_flag:
                        colleague_flag.append(inst)
                    if not any(
                        existing.get("name") == inst for existing in colleague_details
                    ):
                        colleague_details.append({"name": inst})

            # Check if this is the user's own paper
            is_own_paper = False
            for pattern in config.get("self_match", []):
                for author in authors:
                    if pattern.lower() in author.lower():
                        is_own_paper = True
                        break
                if is_own_paper:
                    break

            # Weighted keyword scoring (raw sum)
            matched_keywords = _matched_keywords_for_text(title + " " + abstract, config)
            kw_hits_raw = sum(config["keywords"][kw] for kw in matched_keywords)

            # Capture journal reference if available (e.g. "Nature Astronomy", "Science")
            journal_ref_el = entry.find("{http://arxiv.org/schemas/atom}journal_ref")
            journal_ref = (journal_ref_el.text or "").strip() if journal_ref_el is not None else ""

            papers.append({
                "id": arxiv_id,
                "title": title,
                "abstract": abstract,
                "authors": authors,
                "author_affiliations": author_affiliations,
                "published": published.strftime("%Y-%m-%d"),
                "category": paper_category,
                "url": f"https://arxiv.org/abs/{arxiv_id}",
                "known_authors": known_flag,
                "colleague_matches": colleague_flag,
                "colleague_details": colleague_details,
                "is_own_paper": is_own_paper,
                "matched_keywords": matched_keywords,
                "keyword_hits_raw": kw_hits_raw,
                "journal_ref": journal_ref,
                "feedback_bias": 0,
            })

        if skipped_malformed:
            print(f"  ⚠️  Skipped {skipped_malformed} malformed entries in {category}")
        if skipped_malformed == len(all_entries) and all_entries:
            print(f"  ⚠️  ALL entries from {category} were malformed — arXiv API format may have changed")

    # Deduplicate (same paper may appear in multiple categories)
    seen = set()
    unique = []
    for p in papers:
        if p["id"] not in seen:
            seen.add(p["id"])
            unique.append(p)

    # Normalize keyword scores to 0-100 scale
    # This ensures users with 50 keywords aren't flooded vs users with 5
    max_possible = sum(config["keywords"].values()) or 1
    for p in unique:
        p["keyword_hits"] = round(100 * p["keyword_hits_raw"] / max_possible, 1)

    print(f"  Found {len(unique)} papers total (last {config['days_back']} days)")
    return unique


def _fetch_colleague_papers(
    config: dict[str, Any],
    seen_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Fetch recent papers for each known colleague via arXiv author search.

    Colleagues may publish in categories the user has not subscribed to.
    This targeted search ensures their papers are never missed, regardless
    of which category they appear in.

    Args:
        config: Application configuration (uses colleagues.people, days_back, keywords).
        seen_ids: Optional set of arXiv IDs already fetched; results are deduplicated
                  against this set as well as against each other.

    Returns:
        List of paper dicts (same schema as fetch_arxiv_papers) for colleague papers
        not already present in seen_ids.
    """
    people = config.get("colleagues", {}).get("people", [])
    if not people:
        return []

    already_seen: set[str] = set(seen_ids or [])
    papers: list[dict[str, Any]] = []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    cutoff = datetime.now(timezone.utc) - timedelta(days=config["days_back"])
    max_possible = sum(config["keywords"].values()) or 1

    # Build a deduplicated list of search terms from colleague match patterns.
    # We use the first match pattern for each person as the arXiv au: query term
    # (typically a last name), deduplicating so the same name isn't queried twice.
    query_terms: list[tuple[str, list[dict[str, Any]]]] = []
    seen_terms: set[str] = set()
    for person in people:
        match_patterns = person.get("match", [person.get("name", "")])
        if not match_patterns:
            continue
        term = match_patterns[0].strip()
        if not term or term.lower() in seen_terms:
            continue
        seen_terms.add(term.lower())
        query_terms.append((term, people))

    for i, (term, _) in enumerate(query_terms):
        if i > 0:
            time.sleep(3)  # arXiv etiquette: pause between requests

        params = {
            "search_query": f'au:"{term}"',
            "start": 0,
            "max_results": 25,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
        print(f"  Fetching colleague papers for author '{term}'...")

        req = urllib.request.Request(url)
        req.add_header(
            "User-Agent",
            "arxiv-digest/1.0 (GitHub Actions; https://github.com/SilkeDainese/arxiv-digest)",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                xml_data = response.read().decode("utf-8")
        except Exception as e:
            print(f"  ⚠️  Could not fetch colleague papers for '{term}': {e}")
            continue

        try:
            root = ET.fromstring(xml_data)
        except ET.ParseError as exc:
            print(f"  ⚠️  Failed to parse arXiv XML for colleague '{term}': {exc}")
            continue

        for entry in root.findall("atom:entry", ns):
            try:
                published_str = entry.find("atom:published", ns).text
                published = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
                if published < cutoff:
                    continue

                arxiv_id = entry.find("atom:id", ns).text.split("/abs/")[-1]
                if arxiv_id in already_seen:
                    continue

                title = (entry.find("atom:title", ns).text or "").strip().replace("\n", " ")
                abstract = (entry.find("atom:summary", ns).text or "").strip().replace("\n", " ")
                authors = [
                    a.find("atom:name", ns).text
                    for a in entry.findall("atom:author", ns)
                    if a.find("atom:name", ns) is not None
                    and a.find("atom:name", ns).text
                ]
            except (AttributeError, TypeError, ValueError):
                continue

            # Identify which colleague(s) matched in this paper's author list
            colleague_flag: list[str] = []
            colleague_details: list[dict[str, str]] = []
            for author in authors:
                for colleague in people:
                    for pattern in colleague.get("match", []):
                        if pattern.lower() in author.lower():
                            colleague_name = colleague.get("name", "Unknown")
                            if colleague_name not in colleague_flag:
                                colleague_flag.append(colleague_name)
                            note = str(colleague.get("note", "")).strip()
                            if not any(
                                ex.get("name") == colleague_name
                                for ex in colleague_details
                            ):
                                detail: dict[str, str] = {"name": colleague_name}
                                if note:
                                    detail["note"] = note
                                colleague_details.append(detail)
                            break

            # Check research_authors and self_match the same way as fetch_arxiv_papers
            known_flag = []
            for author in authors:
                for known in config["research_authors"]:
                    if known.lower() in author.lower():
                        known_flag.append(author)
                        break

            is_own_paper = False
            for pattern in config.get("self_match", []):
                for author in authors:
                    if pattern.lower() in author.lower():
                        is_own_paper = True
                        break
                if is_own_paper:
                    break

            matched_keywords = _matched_keywords_for_text(title + " " + abstract, config)
            kw_hits_raw = sum(config["keywords"][kw] for kw in matched_keywords)
            kw_hits = round(100 * kw_hits_raw / max_possible, 1)

            # Use "colleague-fetch" as category sentinel so downstream rendering
            # can display the real category if the XML includes it, or fall back cleanly.
            category_el = entry.find("{http://arxiv.org/schemas/atom}primary_category")
            category = (
                category_el.get("term", "unknown")
                if category_el is not None
                else "unknown"
            )

            already_seen.add(arxiv_id)
            papers.append({
                "id": arxiv_id,
                "title": title,
                "abstract": abstract,
                "authors": authors,
                "author_affiliations": {},
                "published": published.strftime("%Y-%m-%d"),
                "category": category,
                "url": f"https://arxiv.org/abs/{arxiv_id}",
                "known_authors": known_flag,
                "colleague_matches": colleague_flag,
                "colleague_details": colleague_details,
                "is_own_paper": is_own_paper,
                "matched_keywords": matched_keywords,
                "keyword_hits_raw": kw_hits_raw,
                "keyword_hits": kw_hits,
                "feedback_bias": 0,
            })

    if papers:
        print(f"  Found {len(papers)} additional colleague paper(s) outside subscribed categories")
    return papers


def fetch_all_papers(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch papers from subscribed categories AND targeted colleague author searches.

    This is the main fetch entry point for the pipeline. It combines:
    1. Category-based fetch (fetch_arxiv_papers) — the normal subscription feed.
    2. Author-targeted fetch (_fetch_colleague_papers) — ensures colleague papers
       in unsubscribed categories are never missed.

    Results are deduplicated by arXiv ID.

    Args:
        config: Application configuration.

    Returns:
        Deduplicated list of all paper dicts, with keyword scores normalised to 0-100.
    """
    papers = fetch_arxiv_papers(config)
    seen_ids = {p["id"] for p in papers}
    colleague_extras = _fetch_colleague_papers(config, seen_ids=seen_ids)
    # Deduplicate the merged list — _fetch_colleague_papers already respects seen_ids,
    # but a defensive pass here guards against callers (e.g. in tests) that mock
    # _fetch_colleague_papers and return already-seen IDs.
    merged: list[dict[str, Any]] = []
    final_seen: set[str] = set()
    for p in papers + colleague_extras:
        if p["id"] not in final_seen:
            final_seen.add(p["id"])
            merged.append(p)
    return merged


def pre_filter(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep papers that match keywords or have known authors."""
    filtered = [p for p in papers if p["keyword_hits"] > 0 or p["known_authors"] or p.get("feedback_bias", 0) > 0]
    filtered.sort(key=lambda p: (len(p["known_authors"]) * 15 + p["keyword_hits"] + p.get("feedback_bias", 0) * 8), reverse=True)
    if filtered:
        print(f"   {len(filtered)} matched keywords/authors (sending top 30 to AI)")
        return filtered[:30]
    # Discovery mode: no keyword/author matches — prefer astro-ph papers
    print("  No keyword matches — discovery mode: showing newest papers")
    astro = [p for p in papers if p.get("category", "").startswith("astro-ph.")]
    if astro:
        print(f"   Preferring {len(astro)} astro-ph papers in discovery mode")
        discovery = sorted(astro, key=lambda p: p.get("published", ""), reverse=True)
    else:
        discovery = sorted(papers, key=lambda p: p.get("published", ""), reverse=True)
    return discovery[:30]


def extract_colleague_papers(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Separate out papers by colleagues — these always show regardless of score."""
    return [p for p in papers if p.get("colleague_matches")]


def extract_own_papers(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Separate out the user's own papers — these get a celebration section."""
    return [p for p in papers if p.get("is_own_paper")]


# ─────────────────────────────────────────────────────────────
#  AI ANALYSIS — Claude → Keyword Fallback
# ─────────────────────────────────────────────────────────────

def _build_scoring_prompt(paper: dict[str, Any], config: dict[str, Any]) -> str:
    """Build the scoring prompt for Claude."""
    # Sanitize researcher_name to prevent f-string/JSON corruption
    researcher_name = config["researcher_name"].replace('"', "'").replace("{", "").replace("}", "")
    research_context = config.get("research_context", "").strip()
    if not research_context:
        if not config.get("keywords"):
            categories = ", ".join(config.get("categories", []))
            research_context = (
                "No specific research context provided. Score based on general interest "
                f"and novelty for researchers in {categories}. Prioritize: new discoveries, "
                "novel methods, significant results, and papers likely to be widely cited."
            )
        else:
            research_context = "No specific research context provided. Score based on general relevance to the keywords."

    return f"""You are helping curate a personalised arXiv digest for a researcher.

RESEARCHER CONTEXT:
{research_context}

PAPER:
Title: {paper['title']}
Authors: {', '.join(paper['authors'][:8])}
Category: {paper['category']}
Abstract: {paper['abstract']}
Feedback signal: {paper.get('feedback_bias', 0)} (positive means similar keyword matches were previously marked relevant)

Respond with ONLY a valid JSON object (no markdown, no backticks):
{{
  "relevance_score": <integer 1-10>,
  "plain_summary": "<2-3 sentences written peer-to-peer, as one scientist summarising for another — lead with the result or method, not the researchers. Never start with 'Researchers', 'The authors', 'This paper', 'A team', or 'Scientists'. Assume domain knowledge. Example good style: 'New ML approach for stellar Teff from high-res spectra — synthetic MARCS training, recovers within 50K on APOGEE benchmarks. Struggles below [Fe/H] = -2.'>",
  "why_interesting": "<1-2 sentences on why specifically relevant to {researcher_name}'s work>",
  "emoji": "<one relevant emoji>",
  "highlight_phrase": "<punchy 5-8 word headline>",
  "kw_tags": ["<1-3 short keyword tags>"],
  "method_tags": ["<1-3 method tags>"],
  "is_new_catalog": <true or false>,
  "cite_worthy": <true or false>,
  "new_result": "<2-4 word surprising result tag, or null>"
}}

Score generously for this researcher's interests:
10 = core topic (directly related to their main research)
8-9 = closely related work
6-7 = related science they'd want to know about
4-5 = tangentially interesting
1-3 = not relevant to their work
"""


def _default_analysis(paper: dict[str, Any]) -> dict[str, Any]:
    """Fallback analysis fields when AI scoring fails."""
    # keyword_hits is normalized 0-100, map to 1-10 scale
    bias = paper.get("feedback_bias", 0)
    kw_score = round(paper.get("keyword_hits", 0) / 10)
    author_boost = len(paper["known_authors"]) * 3
    feedback_adj = round(bias * 0.4)
    has_signal = paper.get("keyword_hits", 0) > 0 or paper["known_authors"]
    raw_score = kw_score + author_boost + feedback_adj if has_signal else 1
    score = min(10, max(1, raw_score))
    return {
        "relevance_score": score,
        "plain_summary": paper["abstract"][:300] + ("..." if len(paper["abstract"]) > 300 else ""),
        "why_interesting": "Matched your keywords." + (
            f" Known author(s): {', '.join(paper['known_authors'])}." if paper["known_authors"] else ""
        ),
        "emoji": "📄",
        "highlight_phrase": paper["title"][:50],
        "kw_tags": [], "method_tags": [],
        "is_new_catalog": False, "cite_worthy": False, "new_result": None,
    }


def _analyse_with_claude(papers: list[dict[str, Any]], config: dict[str, Any], api_key: str) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Score papers using Claude. Returns (results, error_flag).
    error_flag is None on success, or a string describing the issue."""
    client = anthropic.Anthropic(api_key=api_key)
    analysed = []
    consecutive_failures = 0
    credit_error = False

    for i, paper in enumerate(papers):
        print(f"  Analysing {i+1}/{len(papers)}: {paper['title'][:60]}...")
        prompt = _build_scoring_prompt(paper, config)

        try:
            response = client.messages.create(
                model=config.get("claude_model", "claude-haiku-4-5-20251001"),
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}]
            )
            text = response.content[0].text.strip()
            # Strip markdown fences if Claude wraps with ```json ... ```
            if text.startswith("```"):
                text = re.sub(r"^```[a-z]*\n?", "", text)
                text = re.sub(r"\n?```$", "", text)
            analysis = json.loads(text)
            paper.update(analysis)
            analysed.append(paper)
            consecutive_failures = 0
            print(f"    → score: {analysis.get('relevance_score', '?')}")
        except Exception as e:
            error_str = str(e)
            print(f"    Error: {error_str}")
            consecutive_failures += 1

            # Detect credit/billing errors — no point retrying
            if "credit balance" in error_str.lower() or "billing" in error_str.lower():
                credit_error = True
                print("  ⚠️  Claude API credits exhausted — switching to fallback...")
                # Return remaining papers unscored so the dispatcher can cascade
                return None, "claude_no_credits"

            paper.update(_default_analysis(paper))
            analysed.append(paper)

            # If 3+ consecutive failures, bail out (API might be down)
            if consecutive_failures >= 3:
                print("  ⚠️  3 consecutive Claude failures — switching to fallback...")
                return None, "claude_errors"

    return _filter_and_sort(analysed, config), None



def _analyse_with_vertex_gemini(papers: list[dict[str, Any]], config: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Score papers using Gemini via Vertex AI (ADC auth — no API key needed).
    Returns (results, error_flag). error_flag is None on success, or a string describing the issue."""
    gcp_project = os.environ.get("GCP_PROJECT", "silke-hub")
    client = google_genai.Client(vertexai=True, project=gcp_project, location="europe-west1")
    analysed = []
    consecutive_failures = 0

    for i, paper in enumerate(papers):
        print(f"  Analysing {i+1}/{len(papers)}: {paper['title'][:60]}...")
        prompt = _build_scoring_prompt(paper, config)

        try:
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
            )
            text = response.text.strip()
            # Strip markdown fences if the model wraps with ```json ... ```
            if text.startswith("```"):
                text = re.sub(r"^```[a-z]*\n?", "", text)
                text = re.sub(r"\n?```$", "", text)
            analysis = json.loads(text)
            paper.update(analysis)
            analysed.append(paper)
            consecutive_failures = 0
            print(f"    → score: {analysis.get('relevance_score', '?')}")
        except Exception as e:
            error_str = str(e)
            print(f"    Error: {error_str}")
            consecutive_failures += 1
            paper.update(_default_analysis(paper))
            analysed.append(paper)

            if consecutive_failures >= 3:
                print("  ⚠️  3 consecutive Vertex AI Gemini failures — switching to fallback...")
                return None, "gemini_errors"

    return _filter_and_sort(analysed, config), None


def _fallback_analyse(papers: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Keyword-only scoring when no API key is available."""
    discovery_mode = not config["keywords"]
    for p in papers:
        bias = p.get("feedback_bias", 0)
        if discovery_mode:
            # Discovery mode: score by proxies for significance
            author_score = min(5, len(p["authors"]) // 3)
            known_boost = len(p["known_authors"]) * 3
            score = min(10, max(1, author_score + known_boost + 2 + round(bias * 0.4)))
            why = "Discovery mode — scored by team size and author matches."
        else:
            # keyword_hits is normalized 0-100, map to 1-10 relevance
            score = min(10, max(1, round(p.get("keyword_hits", 0) / 10) + len(p["known_authors"]) * 3 + round(bias * 0.4)))
            why = "Matched your keywords." + (
                f" Known author(s): {', '.join(p['known_authors'])}." if p["known_authors"] else ""
            )
        p.update({
            "relevance_score": score,
            "plain_summary": p["abstract"][:300] + ("..." if len(p["abstract"]) > 300 else ""),
            "why_interesting": why,
            "emoji": "📄",
            "highlight_phrase": p["title"][:50],
            "kw_tags": [], "method_tags": [],
            "is_new_catalog": False, "cite_worthy": False, "new_result": None,
        })
    return _filter_and_sort(papers, config)


def _filter_and_sort(analysed: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Filter by min_score, sort by relevance, cap at max_papers."""
    result = [p for p in analysed if p.get("relevance_score", 0) >= config["min_score"]]
    result.sort(key=lambda p: (p.get("relevance_score", 0) + p.get("feedback_bias", 0) * 0.25), reverse=True)

    dropped = [p for p in analysed if p.get("relevance_score", 0) < config["min_score"]]
    if dropped:
        print(f"   Dropped {len(dropped)} papers below score {config['min_score']}:")
        for p in dropped:
            print(f"     {p.get('relevance_score', 0)}/10 — {p['title'][:60]}")

    return result[:config["max_papers"]]


def analyse_papers(papers: list[dict[str, Any]], config: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Dispatch to Claude → Vertex AI Gemini → keyword fallback.
    Returns (scored_papers, scoring_method) where scoring_method is one of:
    'claude', 'vertex_gemini', 'keywords', or 'keywords_fallback'."""
    if not papers:
        return [], "none"

    api_key_claude = os.environ.get("ANTHROPIC_API_KEY", "").strip()

    if api_key_claude and HAS_ANTHROPIC:
        print("  Using Claude for analysis...")
        result, error = _analyse_with_claude(copy.deepcopy(papers), config, api_key_claude)
        if error is None:
            return result, "claude"
        print("  Claude unavailable — falling back to Vertex AI Gemini...")

    if HAS_VERTEX_GEMINI:
        print("  Using Vertex AI Gemini for analysis...")
        result, error = _analyse_with_vertex_gemini(copy.deepcopy(papers), config)
        if error is None:
            return result, "vertex_gemini"
        print("  Vertex AI Gemini unavailable — falling back to keyword-only scoring")
        return _fallback_analyse(papers, config), "keywords_fallback"

    # No AI available — keyword-only scoring
    print("  ⚠️  No AI available — using keyword-only scoring")
    return _fallback_analyse(papers, config), "keywords"


# ─────────────────────────────────────────────────────────────
#  HTML RENDERING  (email-safe: inline styles + table layout)
# ─────────────────────────────────────────────────────────────

# ── Brand palette ──
from brand import (PINE, GOLD, UMBER, ASH_WHITE, ASH_BLACK,
                   CARD_BORDER, WARM_GREY, PINE_WASH, PINE_LIGHT, GOLD_LIGHT,
                   GOLD_WASH, ALERT_RED, ALERT_RED_WASH, CATALOG_PURPLE, CATALOG_WASH,
                   CREAM, WARM_WHITE, FOOTER_BG, SOFT_GREY, TERRACOTTA, GREEN_HAND,
                   FONT_HEADING, FONT_BODY, FONT_MONO)
from setup.data import AU_STUDENT_TRACK_LABELS, DELIGHT_KEYWORDS


# ── Shared inline-style constants ──
_TAG = f"font-family:'DM Mono',monospace;font-size:10px;letter-spacing:0.1em;text-transform:uppercase;padding:2px 8px;border-radius:3px;display:inline-block;margin:2px 3px 2px 0;color:{WARM_GREY}"


def _esc(text: Any) -> str:
    """HTML-escape a value for safe interpolation into email HTML."""
    return html_mod.escape(str(text)) if text else ""


def _score_bar(score: int | float) -> str:
    """Return a 10-dot bar visualising the relevance score."""
    filled = round(score)
    return "".join(["●" if i < filled else "○" for i in range(10)])


def _accent_color(score: int | float) -> str:
    """Map a relevance score to a brand accent colour."""
    if score >= 9:
        return PINE
    if score >= 7:
        return PINE_LIGHT
    if score >= 5:
        return GOLD
    return UMBER


def _build_tags(p: dict[str, Any]) -> str:
    """Build inline HTML tag spans for a paper card."""
    score = p.get("relevance_score", 5)
    tags = []
    tags.append(f'<span style="{_TAG};background:{PINE_WASH};color:{PINE}">{p["category"]}</span>')
    tags.append(f'<span style="{_TAG};color:{WARM_GREY}">{p["published"]}</span>')
    for a in p.get("known_authors", []):
        tags.append(f'<span style="{_TAG};background:{GOLD_WASH};color:{UMBER}">&#x1F44B; {a}</span>')
    if score >= 9:
        tags.append(f'<span style="{_TAG};background:{ALERT_RED_WASH};color:{ALERT_RED}">&#x1F525; must-read</span>')
    elif score >= 8:
        tags.append(f'<span style="{_TAG};background:{GOLD_WASH};color:{UMBER}">&#x1F4CC; thesis</span>')
    for kw in (p.get("kw_tags") or [])[:2]:
        tags.append(f'<span style="{_TAG};background:{PINE_WASH};color:{PINE}">{_esc(kw)}</span>')
    if p.get("is_new_catalog"):
        tags.append(f'<span style="{_TAG};background:{CATALOG_WASH};color:{CATALOG_PURPLE}">&#x1F4E6; catalog</span>')
    if p.get("cite_worthy"):
        tags.append(f'<span style="{_TAG};background:{PINE_WASH};color:{PINE}">&#x1F4CE; cite this</span>')
    if p.get("new_result"):
        tags.append(f'<span style="{_TAG};background:{PINE_WASH};color:{PINE}">{_esc(p["new_result"])}</span>')
    return " ".join(tags)


def _build_method_tags(p: dict[str, Any]) -> str:
    """Build inline HTML method tag spans for a paper card."""
    return " ".join(f'<span style="{_TAG};background:{GOLD_WASH};color:{UMBER}">{_esc(t)}</span>' for t in (p.get("method_tags") or []))


def _one_sentence(text: str) -> str:
    """Return the first sentence-like chunk, trimmed for compact cards."""
    clean = " ".join((text or "").split())
    if not clean:
        return ""
    # Strip LaTeX math that leaks from abstracts/AI summaries
    clean = _strip_latex(clean)
    clean = re.sub(r"\s*,\s*,", ",", clean)  # collapse ",, " from removed LaTeX
    clean = re.sub(r"of\s*,", "of", clean)  # clean up dangling prepositions
    if not clean:
        return ""
    m = re.match(r"^(.+?[.!?])\s", clean)
    sentence = m.group(1) if m else clean
    if len(sentence) > 180:
        sentence = sentence[:177].rstrip() + "..."
    return sentence


def _strip_latex(text: str) -> str:
    """Remove inline LaTeX from text (titles, summaries)."""
    text = re.sub(r"\$([^$]+)\$", r"\1", text)  # $x$ → x (unwrap math mode)
    text = re.sub(r"\\times", "x", text)  # \times → x
    text = re.sub(r"\\odot", "☉", text)  # \odot → ☉ (solar symbol)
    text = re.sub(r"\\[a-zA-Z]+\{([^}]*)\}", r"\1", text)  # \cmd{x} → x
    text = re.sub(r"\\[a-zA-Z]+", "", text)  # bare \cmd → remove
    text = re.sub(r"[_^]\{([^}]*)\}", r"\1", text)  # _{sub} → sub, ^{sup} → sup
    text = re.sub(r"[_^](\S)", r"\1", text)  # _4 → 4, ^2 → 2
    return " ".join(text.split()).strip()


def _short_title(title: str, max_len: int = 105) -> str:
    """Return a shortened title for denser email cards."""
    t = _strip_latex(" ".join((title or "").split()))
    if len(t) <= max_len:
        return t
    # Cut at word boundary to avoid mid-word truncation
    truncated = t[:max_len]
    last_space = truncated.rfind(" ")
    if last_space > max_len // 2:
        truncated = truncated[:last_space]
    return truncated.rstrip() + "..."


def _build_feedback_links(p: dict[str, Any], github_repo: str) -> str:
    """Build quick-feedback links to prefilled GitHub issues."""
    if not github_repo:
        return ""

    keywords = ", ".join((p.get("matched_keywords") or [])[:8])
    common_body = (
        f"paper_id: {p.get('id', '')}\n"
        f"paper_url: {p.get('url', '')}\n"
        f"category: {p.get('category', '')}\n"
        f"matched_keywords: {keywords}\n"
    )

    rel_title = urllib.parse.quote(f"[digest-feedback] relevant {p.get('id', '')}")
    rel_body = urllib.parse.quote(f"feedback_type: relevant\n{common_body}")
    not_title = urllib.parse.quote(f"[digest-feedback] not_relevant {p.get('id', '')}")
    not_body = urllib.parse.quote(f"feedback_type: not_relevant\n{common_body}")

    rel_url = f"https://github.com/{github_repo}/issues/new?labels=digest-feedback&title={rel_title}&body={rel_body}"
    not_url = f"https://github.com/{github_repo}/issues/new?labels=digest-feedback&title={not_title}&body={not_body}"

    return (
        f'<span style="font-family:\'DM Mono\',monospace;font-size:9px;color:{WARM_GREY};margin-right:8px">Feedback:</span>'
        f'<a href="{rel_url}" title="more like this" style="font-family:\'DM Mono\',monospace;font-size:12px;line-height:1;color:{PINE};text-decoration:none;border:1px solid {PINE};padding:3px 8px;border-radius:3px;margin-right:6px">&#x2191;</a>'
        f'<a href="{not_url}" title="less like this" style="font-family:\'DM Mono\',monospace;font-size:12px;line-height:1;color:{UMBER};text-decoration:none;border:1px solid {GOLD};padding:3px 8px;border-radius:3px">&#x2193;</a>'
    )


def _render_own_paper_section(own_papers: list[dict[str, Any]], researcher_name: str) -> str:
    """Return the 'your paper!' celebration section HTML (or empty string)."""
    if not own_papers:
        return ""

    seen_ids: set[str] = set()
    unique_own: list[dict[str, Any]] = []
    for p in own_papers:
        if p["id"] not in seen_ids:
            seen_ids.add(p["id"])
            unique_own.append(p)

    own_cards = ""
    for p in unique_own:
        authors_short = ", ".join(p["authors"][:4])
        if len(p["authors"]) > 4:
            authors_short += f" +{len(p['authors'])-4}"
        own_cards += f"""
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:10px">
          <tr><td style="background:linear-gradient(135deg, {PINE_WASH}, {GOLD_WASH});border:2px solid {GOLD};border-radius:8px;padding:20px 22px">
            <div style="font-family:'DM Mono',monospace;font-size:10px;letter-spacing:0.2em;text-transform:uppercase;color:{PINE};margin-bottom:8px">&#x1F389; Congratulations, {researcher_name}!</div>
            <div style="font-family:'DM Serif Display',Georgia,serif;font-size:18px;color:{ASH_BLACK};line-height:1.4;margin-bottom:6px">
              <a href="{p['url']}" style="color:{ASH_BLACK};text-decoration:none">{_esc(p['title'])}</a>
            </div>
            <div style="font-family:'DM Mono',monospace;font-size:10px;color:{WARM_GREY};margin-bottom:10px">{_esc(authors_short)}</div>
            <div style="font-family:'IBM Plex Sans',sans-serif;font-size:12px;color:{UMBER};font-style:italic">Your paper appeared on arXiv! &#x1F31F;</div>
          </td></tr>
        </table>"""

    return f"""
  <!-- YOUR PAPERS -->
  <tr><td style="padding:20px 44px 8px;font-family:'DM Mono',monospace;font-size:9px;letter-spacing:0.25em;text-transform:uppercase;color:{PINE}">&#x2500;&#x2500; Your publications &#x1F389; &#x2500;&#x2500;</td></tr>
  <tr><td style="padding:4px 24px 16px">
    {own_cards}
  </td></tr>"""


def _render_colleague_section(colleague_papers: list[dict[str, Any]]) -> str:
    """Return the colleague post-it section HTML (or empty string)."""
    if not colleague_papers:
        return ""

    seen_ids: set[str] = set()
    unique_colleagues: list[dict[str, Any]] = []
    for p in colleague_papers:
        if p["id"] not in seen_ids:
            seen_ids.add(p["id"])
            unique_colleagues.append(p)

    postits = ""
    for p in unique_colleagues:
        details = p.get("colleague_details") or [
            {"name": name} for name in dict.fromkeys(p.get("colleague_matches", []))
        ]
        names = _esc(", ".join(detail["name"] for detail in details if detail.get("name")))
        notes = [
            f"{_esc(detail['name'])} — {_esc(detail['note'])}"
            for detail in details
            if detail.get("name") and detail.get("note")
        ]
        notes_html = ""
        if notes:
            notes_html = "".join(
                f'<div style="font-family:\'IBM Plex Sans\',sans-serif;font-size:11px;color:{UMBER};line-height:1.45;margin-top:4px">{note}</div>'
                for note in notes[:2]
            )
        authors_short = ", ".join(p["authors"][:3])
        if len(p["authors"]) > 3:
            authors_short += f" +{len(p['authors'])-3}"
        postits += f"""
        <table width="48%" cellpadding="0" cellspacing="0" border="0" style="display:inline-table;vertical-align:top;margin:6px 1%">
          <tr><td style="background:{GOLD_WASH};border:1px solid {GOLD_LIGHT};border-radius:6px;padding:14px 16px">
            <div style="font-family:'DM Mono',monospace;font-size:9px;letter-spacing:0.15em;text-transform:uppercase;color:{UMBER};margin-bottom:6px">&#x1F389; {names}</div>
            <div style="font-family:'IBM Plex Sans',sans-serif;font-size:13px;color:{ASH_BLACK};line-height:1.4;margin-bottom:4px">
              <a href="{p['url']}" style="color:{ASH_BLACK};text-decoration:none">{_esc(p['title'][:80])}{'...' if len(p['title']) > 80 else ''}</a>
            </div>
            <div style="font-family:'DM Mono',monospace;font-size:10px;color:{WARM_GREY}">{_esc(authors_short)}</div>
            {notes_html}
          </td></tr>
        </table>"""

    return f"""
  <!-- COLLEAGUE NEWS -->
  <tr><td style="padding:20px 44px 8px;font-family:'DM Mono',monospace;font-size:9px;letter-spacing:0.25em;text-transform:uppercase;color:{UMBER}">&#x2500;&#x2500; Colleague news &#x1F4EC; &#x2500;&#x2500;</td></tr>
  <tr><td style="padding:4px 24px 16px">
    <div style="font-family:'IBM Plex Sans',sans-serif;font-size:12px;color:{WARM_GREY};font-style:italic;margin-bottom:10px">Papers by people you know — send congrats!</div>
    {postits}
  </td></tr>"""


_AU_AFFILIATION_PATTERNS = [
    "aarhus university",
    "aarhus uni",
    "au, denmark",
]


def detect_au_researchers(papers: list[dict[str, Any]]) -> None:
    """Flag papers with Aarhus University affiliated authors."""
    for paper in papers:
        author_affs = paper.get("author_affiliations", {})
        au_authors: list[str] = []
        for author_name, affs in author_affs.items():
            for aff in affs:
                aff_lower = aff.lower()
                if any(pat in aff_lower for pat in _AU_AFFILIATION_PATTERNS):
                    au_authors.append(author_name)
                    break
        paper["is_au_researcher"] = bool(au_authors)
        paper["au_researcher_authors"] = au_authors


def detect_delights(papers: list[dict[str, Any]], max_per_email: int = 2) -> None:
    """Add cultural wayfinding notes to papers for student digest.

    Enriches each paper with a 'delights' list of short notes
    for Kalam-font rendering. Max 1 per paper, max 2 per email.
    AU affiliation delights always get priority over keyword matches.
    """
    email_delight_count = 0

    for paper in papers:
        paper["delights"] = []

        if email_delight_count >= max_per_email:
            continue

        # AU affiliation delights (highest priority — always shown)
        if paper.get("is_au_researcher"):
            au_authors = paper.get("au_researcher_authors", [])
            author_affs = paper.get("author_affiliations", {})
            au_delight = None
            for name in au_authors:
                for aff in author_affs.get(name, []):
                    aff_lower = aff.lower()
                    if "phd" in aff_lower or "student" in aff_lower:
                        au_delight = "AU PhD student"
                        break
                    elif "postdoc" in aff_lower:
                        au_delight = "AU postdoc"
                        break
                if au_delight:
                    break
            if au_delight is None and au_authors:
                au_delight = "AU researcher"
            if au_delight:
                paper["delights"] = [au_delight]
                email_delight_count += 1
                continue  # AU delight is enough for this paper

        if email_delight_count >= max_per_email:
            continue

        # Keyword-based delights from abstract + title (max 1 per paper)
        text = (paper.get("title", "") + " " + paper.get("abstract", "")).lower()
        for keyword, note in DELIGHT_KEYWORDS.items():
            if keyword.lower() in text:
                paper["delights"] = [note]
                email_delight_count += 1
                break


def _render_student_paper_card(p: dict[str, Any]) -> str:
    """Return a student-mode paper entry: 4-line format separated by bottom border."""
    category = p.get("category", "")
    authors_display = ", ".join(p.get("authors", [])[:4])
    if len(p.get("authors", [])) > 4:
        authors_display += f" +{len(p['authors']) - 4}"

    # Line 1: Category label + arXiv code (text only, no pills)
    pkg_ids = p.get("student_package_ids", [])
    pkg_label = AU_STUDENT_TRACK_LABELS.get(pkg_ids[0], "") if pkg_ids else ""
    category_line = ""
    if pkg_label or category:
        label_part = f'<span style="font-family:{FONT_MONO};font-size:11px;color:#999">{_esc(pkg_label)}</span>' if pkg_label else ""
        code_part = f'<span style="font-family:{FONT_MONO};font-size:11px;color:#999;margin-left:6px">{_esc(category)}</span>' if category else ""
        category_line = f'<div style="margin-bottom:4px">{label_part}{code_part}</div>'

    # Line 2: Title (DM Serif Display, linked)
    title_html = (
        f'<div style="font-family:{FONT_HEADING};font-size:19px;color:#2D2D2D;line-height:1.35;margin-bottom:4px">'
        f'<a href="{_esc(p.get("url", ""))}" style="color:inherit;text-decoration:none">{_esc(_short_title(p.get("title", "")))}</a>'
        f'</div>'
    )

    # Line 3: Summary (one sentence)
    summary = _esc(_one_sentence(p.get("plain_summary", "")))
    summary_html = ""
    if summary:
        summary_html = (
            f'<div style="font-family:{FONT_BODY};font-size:14px;color:#555;'
            f'line-height:1.5;margin-bottom:6px">{summary}</div>'
        )

    # Line 4: Bottom meta (authors, date, status, arXiv link)
    pub_date = p.get("published", "")
    meta_parts = [_esc(authors_display)]
    if pub_date:
        meta_parts.append(_esc(pub_date))
    status = p.get("status", "")
    if status:
        meta_parts.append(_esc(status))
    paper_url = p.get("url", "")
    if paper_url:
        meta_parts.append(
            f'<a href="{_esc(paper_url)}" style="color:{PINE_LIGHT};text-decoration:none;text-underline-offset:2px">Read on arXiv &#8594;</a>'
        )
    meta_line = f'<div style="font-family:{FONT_BODY};font-size:12px;color:#999;line-height:1.5">{" &middot; ".join(meta_parts)}</div>'

    # Cultural wayfinding: delight notes (Kalam handscribble)
    delight_html = ""
    for delight in p.get("delights", []):
        delight_html += (
            f'<p style="font-family:\'Kalam\',\'Comic Sans MS\',\'Segoe Print\',cursive;font-size:14px;color:{GREEN_HAND};'
            f'margin:4px 0 0">&#8627; {_esc(delight)}</p>'
        )

    return f"""
    <div style="padding:14px 0;border-bottom:1px solid #E0DDD6">
        {category_line}
        {title_html}
        {summary_html}
        {meta_line}
        {delight_html}
    </div>"""


def _render_student_header(papers: list[dict[str, Any]], date_str: str) -> str:
    """Return the student digest email header with pine bar."""
    return f"""
  <tr><td style="background:{PINE};padding:14px 28px">
    <table width="100%" cellpadding="0" cellspacing="0" border="0">
      <tr>
        <td style="font-family:{FONT_HEADING};font-size:18px;color:{WARM_WHITE}">AU student digest</td>
        <td style="text-align:right;font-family:{FONT_MONO};font-size:11px;color:rgba(255,253,248,0.7)">{date_str}</td>
      </tr>
    </table>
  </td></tr>
  <tr><td style="padding:24px 28px 16px">
    <div style="font-family:{FONT_HEADING};font-size:24px;color:{ASH_BLACK};margin-bottom:4px">Your papers this week</div>
    <div style="font-family:{FONT_MONO};font-size:12px;color:{WARM_GREY}">{len(papers)} paper{"s" if len(papers) != 1 else ""} selected for you</div>
  </td></tr>"""


def _render_paper_card(p: dict[str, Any], is_top_pick: bool, total_papers: int, github_repo: str) -> str:
        """Return a compact deep-read HTML card for a single paper."""
        score = p.get("relevance_score", 5)
        ac = _accent_color(score)
        authors_display = ", ".join(p["authors"][:4])
        if len(p["authors"]) > 4:
                authors_display += f" +{len(p['authors'])-4}"

        top_label = ""
        if is_top_pick and total_papers > 1:
                top_label = (
                        f'<span style="font-family:\'DM Mono\',monospace;font-size:9px;letter-spacing:0.2em;'
                        f'text-transform:uppercase;background:{PINE};color:white;padding:3px 10px;'
                        f'display:inline-block;border-radius:3px;margin-bottom:8px">&#x2B51; Top pick</span>'
                )

        what_changed = _esc(_one_sentence(p.get("plain_summary", "")))
        feedback_links = _build_feedback_links(p, github_repo)
        emoji = _esc(p.get('emoji', '')) or '&#x1F52D;'
        why = _esc(p.get('why_interesting', ''))

        return f"""
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:12px">
            <tr><td style="background:white;border:1px solid {CARD_BORDER};border-left:4px solid {ac};border-radius:8px;padding:16px 18px 14px">
                {top_label}
                <table width="100%" cellpadding="0" cellspacing="0" border="0">
                    <tr>
                        <td style="vertical-align:top;padding-bottom:8px">{_build_tags(p)}</td>
                        <td width="78" style="vertical-align:top;text-align:right;padding-bottom:8px">
                            <span style="font-size:22px;line-height:1">{emoji}</span><br>
                            <span style="font-family:'DM Serif Display',Georgia,serif;font-size:22px;color:{ac};line-height:1">{score}</span><span style="font-size:12px;color:{WARM_GREY}">/10</span><br>
                            <span style="font-family:'DM Mono',monospace;font-size:8px;letter-spacing:2px;color:{ac};opacity:0.55">{_score_bar(score)}</span>
                        </td>
                    </tr>
                </table>
                <div style="font-family:'IBM Plex Sans',sans-serif;font-size:15px;font-weight:600;color:{ASH_BLACK};line-height:1.35;margin-bottom:4px"><a href="{p['url']}" style="color:{ASH_BLACK};text-decoration:none">{_esc(_short_title(p['title']))}</a></div>
                <div style="font-family:'DM Mono',monospace;font-size:10px;color:{WARM_GREY};margin-bottom:8px">{_esc(authors_display)}</div>
                <div style="font-family:'IBM Plex Sans',sans-serif;font-size:12px;color:{ASH_BLACK};line-height:1.55;margin:0 0 10px"><strong>What changed:</strong> {what_changed}</div>
                <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:12px">
                    <tr><td style="background:{GOLD_WASH};border:1px solid {GOLD_LIGHT};border-radius:5px;padding:10px 12px">
                        <div style="font-family:'DM Mono',monospace;font-size:9px;letter-spacing:0.2em;text-transform:uppercase;color:{WARM_GREY};margin-bottom:6px">&#x2B50; Why it matters to you</div>
                        <div style="font-family:'IBM Plex Sans',sans-serif;font-size:12px;color:{UMBER};line-height:1.65">{why}</div>
                    </td></tr>
                </table>
                <table width="100%" cellpadding="0" cellspacing="0" border="0">
                    <tr>
                        <td style="vertical-align:middle">{feedback_links}</td>
                        <td style="vertical-align:middle;text-align:right"><a href="{p['url']}" style="font-family:{FONT_MONO};font-size:10px;letter-spacing:0.1em;text-transform:uppercase;color:{PINE};text-decoration:none;border:1px solid {PINE};padding:6px 12px;border-radius:3px;display:inline-block;white-space:nowrap">Read paper &#x2192;</a></td>
                    </tr>
                </table>
            </td></tr>
        </table>"""


def _render_skim_card(p: dict[str, Any], github_repo: str) -> str:
        """Return the compact 5-minute skim card (one-line summary)."""
        score = p.get("relevance_score", 5)
        ac = _accent_color(score)
        summary = _esc(_one_sentence(p.get("plain_summary", "")))
        feedback_links = _build_feedback_links(p, github_repo)

        return f"""
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:10px">
            <tr><td style="background:white;border:1px solid {CARD_BORDER};border-left:4px solid {ac};border-radius:7px;padding:12px 14px">
                <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:4px">
                    <tr>
                        <td style="font-family:{FONT_BODY};font-size:14px;font-weight:600;line-height:1.35;color:{ASH_BLACK}">
                            <a href="{p['url']}" style="color:{ASH_BLACK};text-decoration:none">{_esc(_short_title(p['title'], 90))}</a>
                        </td>
                        <td width="50" style="font-family:{FONT_MONO};font-size:10px;color:{ac};white-space:nowrap;text-align:right;vertical-align:top">{score}/10</td>
                    </tr>
                </table>
                <div style="font-family:'IBM Plex Sans',sans-serif;font-size:12px;color:{ASH_BLACK};line-height:1.5;margin-bottom:8px">{summary}</div>
                <table width="100%" cellpadding="0" cellspacing="0" border="0">
                    <tr>
                        <td style="vertical-align:middle">{feedback_links}</td>
                        <td style="vertical-align:middle;text-align:right"><span style="font-family:{FONT_MONO};font-size:9px;color:{WARM_GREY}">{p.get('category', '')}</span></td>
                    </tr>
                </table>
            </td></tr>
        </table>"""


def _render_scoring_notice(scoring_method: str) -> str:
    """Return the scoring-method notice banner HTML (or empty string)."""
    if scoring_method == "vertex_gemini":
        return f"""
  <tr><td style="padding:12px 44px">
    <div style="background:{PINE_WASH};border:1px solid {CARD_BORDER};border-radius:6px;padding:14px 18px;font-family:'IBM Plex Sans',sans-serif;font-size:12px;color:{WARM_GREY};text-align:center">
      &#x1F916; Papers scored by <strong>Gemini 2.0 Flash (Vertex AI / GCP)</strong>. High-quality AI scoring via Google Cloud.
    </div>
  </td></tr>"""
    elif scoring_method == "keywords_fallback":
        return f"""
  <tr><td style="padding:12px 44px">
    <div style="background:{GOLD_WASH};border:1px solid {GOLD_LIGHT};border-radius:6px;padding:14px 18px;font-family:'IBM Plex Sans',sans-serif;font-size:12px;color:{UMBER};text-align:center">
      &#x26A0;&#xFE0F; <strong>AI scoring unavailable this run</strong> — your API key may be out of credits or the API was unreachable. Papers were scored by keyword matching only, so relevance scores may be less accurate. Top up at <a href="https://console.anthropic.com" style="color:{PINE}">console.anthropic.com</a>.
    </div>
  </td></tr>"""
    elif scoring_method == "keywords":
        return f"""
    <tr><td style="padding:12px 44px">
        <div style="background:{PINE_WASH};border:1px solid {CARD_BORDER};border-radius:6px;padding:14px 18px;font-family:'IBM Plex Sans',sans-serif;font-size:12px;color:{WARM_GREY};text-align:center">
            &#x1F4CA; Papers scored by keyword matching (no AI key configured). For smarter scoring, add an <code>ANTHROPIC_API_KEY</code> to your repo secrets — $5 of credits will last hundreds of digests.
        </div>
    </td></tr>"""
    return ""


def _render_header(papers: list[dict[str, Any]], colleague_papers: list[dict[str, Any]],
                   config: dict[str, Any], date_str: str, researcher_name: str,
                   digest_name: str) -> str:
    """Return the email header HTML including stats row."""
    avg_score = round(sum(p.get("relevance_score", 0) for p in papers) / max(len(papers), 1), 1)
    known_count = sum(1 for p in papers if p.get("known_authors"))
    colleague_count = len(set(p["id"] for p in colleague_papers)) if colleague_papers else 0

    stats_cells = f"""
        <td style="padding-right:36px">
          <div style="font-family:'DM Serif Display',Georgia,serif;font-size:32px;color:{PINE};line-height:1">{len(papers)}</div>
          <div style="font-family:'DM Mono',monospace;font-size:9px;text-transform:uppercase;letter-spacing:0.18em;color:{WARM_GREY};margin-top:3px">{"paper" if len(papers) == 1 else "papers"} curated</div>
        </td>
        <td style="padding-right:36px">
          <div style="font-family:'DM Serif Display',Georgia,serif;font-size:32px;color:{PINE};line-height:1">{known_count}</div>
          <div style="font-family:'DM Mono',monospace;font-size:9px;text-transform:uppercase;letter-spacing:0.18em;color:{WARM_GREY};margin-top:3px">familiar authors</div>
        </td>
        <td style="padding-right:36px">
          <div style="font-family:'DM Serif Display',Georgia,serif;font-size:32px;color:{PINE};line-height:1">{avg_score}</div>
          <div style="font-family:'DM Mono',monospace;font-size:9px;text-transform:uppercase;letter-spacing:0.18em;color:{WARM_GREY};margin-top:3px">avg relevance</div>
        </td>"""
    if colleague_count:
        stats_cells += f"""
        <td>
          <div style="font-family:'DM Serif Display',Georgia,serif;font-size:32px;color:{PINE};line-height:1">{colleague_count}</div>
          <div style="font-family:'DM Mono',monospace;font-size:9px;text-transform:uppercase;letter-spacing:0.18em;color:{WARM_GREY};margin-top:3px">colleague papers</div>
        </td>"""

    cats_display = " &middot; ".join(config["categories"])

    return f"""
  <!-- HEADER -->
  <tr><td style="padding:52px 44px 36px;background:white;border-bottom:1px solid {CARD_BORDER}">
    <div style="font-family:'DM Mono',monospace;font-size:10px;letter-spacing:0.3em;text-transform:uppercase;color:{PINE};margin-bottom:14px;opacity:0.75">arXiv &middot; {cats_display} &middot; {date_str}</div>
    <div style="font-family:'DM Serif Display',Georgia,serif;font-size:44px;font-weight:700;line-height:1.05;color:{ASH_BLACK};margin-bottom:6px">{digest_name}{"" if researcher_name.split()[0].lower() in digest_name.lower() else f'<br>for <span style="font-style:italic;color:{PINE}">{researcher_name}</span>'}</div>
    <div style="font-size:13px;color:{WARM_GREY};margin-top:10px;font-family:'DM Serif Display',Georgia,serif">Your window into the cosmos &#x2726; curated by AI</div>
    <!-- Stats row -->
    <table cellpadding="0" cellspacing="0" border="0" style="margin-top:28px">
      <tr>{stats_cells}</tr>
    </table>
    <div style="margin-top:20px;font-family:'DM Mono',monospace;font-size:10px;color:{CARD_BORDER};letter-spacing:0.08em;border-top:1px solid {CARD_BORDER};padding-top:16px">Monitoring {cats_display} &middot; last {config['days_back']} days &middot; threshold &#x2265; {config['min_score']}/10</div>
  </td></tr>"""


def _render_own_key_nudge(config: dict[str, Any], scoring_method: str) -> str:
    """Gentle nudge to get your own API key if using the shared community key."""
    if config.get("own_api_key"):
        return ""
    if scoring_method not in {"claude", "vertex_gemini", "keywords_fallback"}:
        return ""
    github_repo = config.get("github_repo", "")
    secrets_url = f"https://github.com/{github_repo}/settings/secrets/actions" if github_repo else ""
    secrets_link = f' <a href="{secrets_url}" style="color:{PINE};text-decoration:none">Add it to your repo secrets</a> &rarr;' if secrets_url else ""
    return f"""
  <tr><td style="padding:8px 44px 0">
    <div style="background:{PINE_WASH};border:1px solid {CARD_BORDER};border-radius:6px;padding:12px 18px;font-family:'IBM Plex Sans',sans-serif;font-size:11px;color:{WARM_GREY};text-align:center">
      &#x1F511; You are using a shared AI key — it works, but may be slower when many people run their digests at the same time. Add your own <code>ANTHROPIC_API_KEY</code> for faster, more reliable scoring.{secrets_link}
    </div>
  </td></tr>"""


def _render_report_link(github_repo: str) -> str:
    """Return a small 'Something wrong?' link pointing to a pre-filled GitHub issue."""
    if not github_repo:
        return ""
    repo_name = github_repo.split("/")[-1] if "/" in github_repo else github_repo
    issue_url = (
        "https://github.com/SilkeDainese/arxiv-digest/issues/new"
        f"?title=Digest+issue&body=Fork:+{urllib.parse.quote(repo_name)}"
    )
    return f"""
    <div style="font-family:'DM Mono',monospace;font-size:9px;color:{WARM_GREY};letter-spacing:0.08em;text-align:center;margin-top:10px">
      Something wrong? <a href="{issue_url}" style="color:{PINE};text-decoration:none">Report an issue &#x2192;</a>
    </div>"""


def _render_student_footer(config: dict[str, Any], scoring_method: str) -> str:
    """Return the student-specific email footer with personal attribution."""
    subscription_manage_url = str(config.get("subscription_manage_url", "")).strip()
    subscription_unsubscribe_url = str(config.get("subscription_unsubscribe_url", "")).strip()
    link_style = f"color:{PINE};text-decoration:none"

    service_links: list[str] = []
    if subscription_manage_url:
        service_links.append(f'<a href="{subscription_manage_url}" style="{link_style}">&#x2699;&#xFE0F; Change settings</a>')
        service_links.append(f'<a href="{subscription_manage_url}" style="{link_style}">&#x1F4DD; Change categories</a>')
    if subscription_unsubscribe_url:
        service_links.append(f'<a href="{subscription_unsubscribe_url}" style="color:{SOFT_GREY};text-decoration:none">Unsubscribe</a>')

    self_service_html = f"""
    <div style="font-family:'DM Mono',monospace;font-size:10px;color:{WARM_GREY};letter-spacing:0.08em;margin-bottom:14px;text-align:center">
      {' &middot; '.join(service_links)}
    </div>""" if service_links else ""

    scoring_labels = {
        "claude": "Claude Haiku (Anthropic)",
        "vertex_gemini": "Gemini 2.0 Flash (Vertex AI / GCP)",
        "keywords": "keyword matching",
        "keywords_fallback": "keyword matching (AI unavailable)",
        "none": "AI",
    }
    scoring_label = scoring_labels.get(scoring_method, "AI")

    return f"""
  <!-- FOOTER -->
  <tr><td style="padding:36px 44px;border-top:1px solid {CARD_BORDER};background:{FOOTER_BG}">
    <div style="text-align:center;font-size:18px;margin-bottom:18px;opacity:0.5;letter-spacing:10px;color:{WARM_GREY}">&#x2726; &middot; &#x2726; &middot; &#x2726;</div>
    {self_service_html}
    <div style="font-family:'DM Mono',monospace;font-size:9.5px;color:{WARM_GREY};letter-spacing:0.1em;line-height:2.2;text-align:center">
      Made by <a href="https://silkedainese.github.io" style="color:{WARM_GREY};text-decoration:none">Silke Dainese</a> &middot;
      <a href="mailto:dainese@phys.au.dk" style="color:{WARM_GREY};text-decoration:none">dainese@phys.au.dk</a><br>
      Summaries are AI-generated and may contain errors.<br>
      This digest is a personal project by Silke Dainese and is not affiliated with Aarhus University.
    </div>
  </td></tr>"""


def _render_footer(config: dict[str, Any], scoring_method: str) -> str:
    """Return the email footer HTML including self-service links and credits."""
    digest_name = config.get("digest_name", "arXiv Digest")
    institution = config.get("institution", "")
    department = config.get("department", "")
    tagline = config.get("tagline", "")
    github_repo = config.get("github_repo", "")

    # ── Footer location line ──
    location_parts = [digest_name]
    if institution:
        location_parts.append(institution)
    if department:
        location_parts.append(department)
    location_line = " &middot; ".join(location_parts)

    tagline_line = f'<em>"{tagline}"</em>' if tagline else ""

    # ── Self-service links ──
    setup_url = config.get("setup_url", DEFAULT_SETUP_URL)
    subscription_manage_url = str(config.get("subscription_manage_url", "")).strip()
    subscription_unsubscribe_url = str(config.get("subscription_unsubscribe_url", "")).strip()
    link_style = f"color:{PINE};text-decoration:none"
    service_links: list[str] = []

    if github_repo:
        edit_url = f"https://github.com/{github_repo}/edit/main/config.yaml"
        pause_url = f"https://github.com/{github_repo}/actions"
        delete_url = f"https://github.com/{github_repo}/settings#danger-zone"
        service_links.append(f'<a href="{edit_url}" style="{link_style}">&#x2699;&#xFE0F; Configure keywords</a>')
        service_links.append(f'<a href="{setup_url}" style="{link_style}">&#x1F504; Re-run setup wizard</a>')
        service_links.append(f'<a href="{pause_url}" style="{link_style}">&#x23F8;&#xFE0F; Pause digest</a>')
        service_links.append(f'<a href="{delete_url}" style="{link_style}">&#x1F5D1;&#xFE0F; Unsubscribe &amp; delete</a>')
    elif subscription_manage_url:
        service_links.append(f'<a href="{subscription_manage_url}" style="{link_style}">&#x2699;&#xFE0F; Change settings</a>')
        service_links.append(f'<a href="{subscription_manage_url}" style="{link_style}">&#x1F4DD; Change categories</a>')
        service_links.append(f'<a href="{subscription_manage_url}" style="{link_style}">&#x1F4CB; Manage subscription</a>')
        if subscription_unsubscribe_url:
            service_links.append(f'<a href="{subscription_unsubscribe_url}" style="{link_style}">&#x1F5D1;&#xFE0F; Unsubscribe</a>')
    else:
        service_links.append(f'<a href="{setup_url}" style="{link_style}">&#x2699;&#xFE0F; Edit preferences</a>')

    self_service_html = f"""
    <div style="font-family:'DM Mono',monospace;font-size:10px;color:{WARM_GREY};letter-spacing:0.08em;margin-bottom:14px;text-align:center">
      {' &middot; '.join(service_links)}
    </div>"""

    # ── Scoring label ──
    scoring_labels = {
        "claude": "Claude Haiku (Anthropic)",
        "vertex_gemini": "Gemini 2.0 Flash (Vertex AI / GCP)",
        "keywords": "keyword matching",
        "keywords_fallback": "keyword matching (AI unavailable)",
        "none": "AI",
    }
    scoring_label = scoring_labels.get(scoring_method, "AI")

    return f"""
  <!-- FOOTER -->
  <tr><td style="padding:36px 44px;border-top:1px solid {CARD_BORDER};background:white">
    <div style="text-align:center;font-size:18px;margin-bottom:18px;opacity:0.5;letter-spacing:10px;color:{WARM_GREY}">&#x2726; &middot; &#x2726; &middot; &#x2726;</div>
    {self_service_html}
    <div style="font-family:'DM Mono',monospace;font-size:9.5px;color:{WARM_GREY};letter-spacing:0.1em;line-height:2.2;text-align:center">
      {location_line}<br>
      Papers sourced from <a href="https://arxiv.org" style="color:{PINE};text-decoration:none">arxiv.org</a> &middot; Summaries by {scoring_label} &middot; Running on GitHub Actions<br>
      {tagline_line}
    </div>
    <div style="font-family:'DM Mono',monospace;font-size:9px;color:{CARD_BORDER};letter-spacing:0.08em;line-height:2;text-align:center;margin-top:12px;border-top:1px solid {CARD_BORDER};padding-top:12px">
      Built by <a href="https://silkedainese.github.io" style="color:{WARM_GREY};text-decoration:none">Silke S. Dainese</a> &middot;
      <a href="mailto:dainese@phys.au.dk" style="color:{WARM_GREY};text-decoration:none">dainese@phys.au.dk</a> &middot;
      <a href="https://github.com/SilkeDainese" style="color:{WARM_GREY};text-decoration:none">github.com/SilkeDainese</a>
    </div>
    {_render_report_link(github_repo)}
  </td></tr>"""


def _render_css(digest_name: str, researcher_name: str, date_str: str) -> str:
    """Return the HTML head with font imports and opening body/table tags."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{digest_name}{'' if researcher_name.split()[0].lower() in digest_name.lower() else f' for {researcher_name}'} — {date_str}</title>
</head>
<body style="margin:0;padding:0;background:{ASH_WHITE};color:{ASH_BLACK};font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif;font-weight:300;line-height:1.7;-webkit-font-smoothing:antialiased">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{ASH_WHITE}">
<tr><td align="center">
<table width="680" cellpadding="0" cellspacing="0" border="0" style="max-width:680px;width:100%;background:{ASH_WHITE}">"""


def render_html(papers: list[dict[str, Any]], colleague_papers: list[dict[str, Any]],
                config: dict[str, Any], date_str: str,
                own_papers: list[dict[str, Any]] | None = None,
                scoring_method: str = "claude") -> str:
    """Render the full HTML digest email from scored papers and config."""

    researcher_name = config.get("researcher_name", "Reader")
    digest_name = config.get("digest_name", "arXiv Digest")
    recipient_view_mode = config.get("recipient_view_mode", "deep_read")
    github_repo = config.get("github_repo", "")
    student_mode = bool(config.get("subscription_manage_url"))

    if own_papers is None:
        own_papers = []

    # ── Build paper cards ──
    cards_html = ""
    displayed_papers = papers
    if student_mode:
        for p in displayed_papers:
            cards_html += _render_student_paper_card(p)
    elif recipient_view_mode == "5_min_skim":
        displayed_papers = papers[:3]
        for p in displayed_papers:
            cards_html += _render_skim_card(p, github_repo)
    else:
        for i, p in enumerate(displayed_papers):
            cards_html += _render_paper_card(
                p,
                is_top_pick=(i == 0),
                total_papers=len(displayed_papers),
                github_repo=github_repo,
            )

    if not papers and not colleague_papers:
        cards_html = f'<div style="text-align:center;padding:60px 24px;color:{WARM_GREY};font-family:\'DM Serif Display\',Georgia,serif;font-style:italic;font-size:18px">No highly relevant papers this period. All quiet on the arXiv front. &#x2615;</div>'

    # ── Student mode uses simpler layout ──
    if student_mode:
        welcome_html = ""
        if config.get("show_welcome"):
            welcome_html = """
  <tr><td>
    <div style="padding:24px 28px;background:#F5F3EF;border-bottom:1px solid #E0DDD6">
      <p style="font-family:'DM Serif Display',serif;font-size:22px;color:#2D2D2D;margin:0 0 8px">
        Welcome to the AU student digest
      </p>
      <p style="font-family:'IBM Plex Sans',sans-serif;font-size:14px;color:#555;line-height:22px;margin:0">
        Each week, you'll get papers from your selected categories.
        Scan the titles &mdash; that's all most researchers do too.
        You can change your categories or unsubscribe anytime from the footer.
      </p>
    </div>
  </td></tr>
"""
        return (
            _render_css(digest_name, researcher_name, date_str)
            + "\n"
            + _render_student_header(papers, date_str)
            + "\n"
            + welcome_html
            + f"""
  <!-- PAPER CARDS -->
  <tr><td style="padding:12px 24px 24px">
    {cards_html}
  </td></tr>
"""
            + _render_student_footer(config, scoring_method)
            + """

</table>
</td></tr>
</table>
</body>
</html>"""
        )

    # ── Assemble full document (researcher mode) ──
    return (
        _render_css(digest_name, researcher_name, date_str)
        + "\n"
        + _render_header(papers, colleague_papers, config, date_str, researcher_name, digest_name)
        + "\n"
        + f"""
  {_render_own_paper_section(own_papers, researcher_name)}

  {_render_colleague_section(colleague_papers)}

  <!-- SECTION DIVIDER -->
    <tr><td style="padding:20px 44px 14px;font-family:'DM Mono',monospace;font-size:9px;letter-spacing:0.25em;text-transform:uppercase;color:{WARM_GREY}">&#x2500;&#x2500; {"5-minute skim" if recipient_view_mode == "5_min_skim" else "All papers this edition"} &middot; {len(displayed_papers)} {"paper" if len(displayed_papers) == 1 else "papers"} &#x2500;&#x2500;</td></tr>

  <!-- PAPER CARDS -->
  <tr><td style="padding:0 24px 52px">
    {cards_html}
  </td></tr>

  {_render_scoring_notice(scoring_method)}

  {_render_own_key_nudge(config, scoring_method)}
"""
        + _render_footer(config, scoring_method)
        + """

</table>
</td></tr>
</table>
</body>
</html>"""
    )


def _parse_recipient_emails(value: Any) -> list[str]:
    """Return a de-duplicated recipient list from a string or sequence."""
    if value is None:
        return []

    if isinstance(value, str):
        raw_parts = re.split(r"[\n,;]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_parts = []
        for item in value:
            raw_parts.extend(re.split(r"[\n,;]+", str(item)))
    else:
        raw_parts = [str(value)]

    recipients: list[str] = []
    seen: set[str] = set()
    for part in raw_parts:
        email = part.strip()
        if not email:
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        recipients.append(email)
    return recipients


# ─────────────────────────────────────────────────────────────
#  EMAIL SENDING — Relay (default) or direct SMTP
# ─────────────────────────────────────────────────────────────

RELAY_URL = os.environ.get(
    "DIGEST_RELAY_URL",
    "https://arxiv-digest-relay.vercel.app/api/send",
)


def _get_relay_token() -> str:
    """Return the relay token or an empty string if not configured."""
    return os.environ.get("DIGEST_RELAY_TOKEN", "").strip()


def _build_plain_text(date_str: str, paper_count: int,
                      papers: list[dict[str, Any]] | None) -> str:
    """Build an informative plain-text fallback for the email."""
    lines = [f"Your arXiv digest for {date_str} — {paper_count} papers.\n"]
    if papers:
        for p in papers[:10]:
            score = p.get("relevance_score", "?")
            lines.append(f"  [{score}/10] {p.get('title', 'Untitled')}")
            lines.append(f"         {p.get('url', '')}")
        if len(papers) > 10:
            lines.append(f"  ... and {len(papers) - 10} more")
    lines.append("\nOpen in a browser for the full experience.")
    return "\n".join(lines)


def _send_via_relay(recipients: list[str], subject: str,
                    html: str, plain_text: str) -> bool:
    """Send email via the shared relay service. Returns True on success."""
    relay_token = _get_relay_token()
    if not relay_token:
        print("❌ Relay send failed: DIGEST_RELAY_TOKEN is not configured.")
        print("   Add DIGEST_RELAY_TOKEN to your repo secrets or configure SMTP_USER/SMTP_PASSWORD instead.")
        return False

    payload = json.dumps({
        "token": relay_token,
        "recipients": recipients,
        "subject": subject,
        "html": html,
        "plain_text": plain_text,
    }).encode()

    req = urllib.request.Request(
        RELAY_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                if len(recipients) == 1:
                    print(f"✅ Email sent to {recipients[0]} via relay")
                else:
                    print(f"✅ Email sent to {len(recipients)} recipients via relay")
                return True
            print(f"❌ Relay returned error: {result.get('error', 'unknown')}")
            return False
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            print("❌ Relay token is invalid or expired.")
            print("   Re-run the setup wizard or ask the maintainer for a new token.")
        elif exc.code == 429:
            print("❌ Relay rate limit reached. Your digest will retry on the next scheduled run.")
        else:
            print(f"❌ Relay returned HTTP {exc.code}.")
        return False
    except urllib.error.URLError as exc:
        print(f"❌ Could not reach relay: {exc.reason}")
        return False
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"❌ Relay returned unexpected response: {exc}")
        return False


def _send_via_smtp(recipients: list[str], subject: str, html: str,
                   plain_text: str, smtp_user: str, smtp_password: str,
                   smtp_server: str, smtp_port: int,
                   digest_name: str) -> bool:
    """Send email via direct SMTP. Returns True on success."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{digest_name} <{smtp_user}>"
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, recipients, msg.as_string())
        if len(recipients) == 1:
            print(f"✅ Email sent to {recipients[0]} via {smtp_server}")
        else:
            print(f"✅ Email sent to {len(recipients)} recipients via {smtp_server}")
        return True
    except smtplib.SMTPAuthenticationError as e:
        print(f"❌ SMTP auth failed: {e}")
        if "gmail" in smtp_server.lower():
            print("   Make sure SMTP_PASSWORD is a Gmail App Password, not your regular password.")
            print("   Generate one at: Google Account > Security > 2-Step Verification > App passwords")
        elif "office365" in smtp_server.lower():
            print("   For Office 365, use an App Password from your Microsoft account security settings.")
        return False
    except Exception as e:
        print(f"❌ Email send failed: {e}")
        print("📋 Digest was saved as digest_output.html artifact — check Actions artifacts to download it.")
        return False


def send_email(html: str, paper_count: int, date_str: str, config: dict[str, Any],
               papers: list[dict[str, Any]] | None = None,
               subject_prefix: str = "") -> bool:
    """Send the digest email via relay (default) or direct SMTP.

    Uses the shared relay service unless SMTP_USER and SMTP_PASSWORD are set
    as environment variables, in which case it sends directly.
    """
    recipients = _parse_recipient_emails(config.get("recipient_email"))
    if not recipients:
        print("⚠️  No recipient email configured — skipping email send.")
        return False

    digest_name = config["digest_name"]
    paper_word = "paper" if paper_count == 1 else "papers"
    subject = f"{subject_prefix}🔭 {digest_name} — {paper_count} {paper_word} · {date_str}"
    plain_text = _build_plain_text(date_str, paper_count, papers)

    # If SMTP credentials are set, send directly (self-hosted mode)
    smtp_user = os.environ.get("SMTP_USER", "").strip() or os.environ.get("GMAIL_USER", "").strip()
    smtp_password = os.environ.get("SMTP_PASSWORD", "").strip() or os.environ.get("GMAIL_APP_PASSWORD", "").strip()

    if smtp_user and smtp_password:
        return _send_via_smtp(recipients, subject, html, plain_text,
                              smtp_user, smtp_password,
                              config["smtp_server"], config["smtp_port"],
                              digest_name)
    return _send_via_relay(recipients, subject, html, plain_text)


# ─────────────────────────────────────────────────────────────
#  FAILURE NOTIFICATIONS
# ─────────────────────────────────────────────────────────────

def send_failure_report(config: dict[str, Any] | None, error_summary: str) -> None:
    """Send a plain-text failure email to the configured recipient.

    Tries direct SMTP first (works even if the relay is down), then falls back
    to the relay.  If email is not configured at all, prints to stderr only.
    """
    recipient = (config or {}).get("recipient_email", "")
    if isinstance(recipient, list):
        recipient = recipient[0] if recipient else ""
    recipient = (recipient or "").strip()
    if not recipient:
        print("⚠️  No recipient_email configured — failure report printed to stderr only.", file=sys.stderr)
        return

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject = f"⚠️ arXiv Digest failed — {date_str}"
    body = (
        f"Your arXiv Digest pipeline failed on {date_str}.\n\n"
        f"Error details:\n{error_summary}\n\n"
        "Check the GitHub Actions run log for the full traceback.\n"
        "If this keeps happening, open an issue: "
        "https://github.com/SilkeDainese/arxiv-digest/issues\n"
    )

    smtp_user = os.environ.get("SMTP_USER", "").strip() or os.environ.get("GMAIL_USER", "").strip()
    smtp_password = os.environ.get("SMTP_PASSWORD", "").strip() or os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    smtp_server = (config or {}).get("smtp_server", "smtp.gmail.com")
    smtp_port = int((config or {}).get("smtp_port", 587))
    digest_name = (config or {}).get("digest_name", "arXiv Digest")

    # Try direct SMTP first — works even when the relay is down
    if smtp_user and smtp_password:
        ok = _send_via_smtp(
            [recipient], subject, body, body,
            smtp_user, smtp_password, smtp_server, smtp_port, digest_name,
        )
        if ok:
            return
        print("⚠️  Direct SMTP failure notification failed — trying relay.", file=sys.stderr)

    # Relay fallback
    relay_token = _get_relay_token()
    if relay_token:
        _send_via_relay([recipient], subject, body, body)
        return

    print(
        f"⚠️  Could not send failure notification (no SMTP or relay configured).\n"
        f"    Subject: {subject}\n"
        f"    Body: {body}",
        file=sys.stderr,
    )


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────

def main() -> None:
    """Run the full arXiv digest pipeline: fetch, score, render, and email."""
    preview_mode = "--preview" in sys.argv

    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    print(f"\n🔭 arXiv Digest — {date_str}")
    if preview_mode:
        print("   (preview mode — no email will be sent)")
    print("=" * 50)

    print("\n📋 Loading config.yaml...")
    try:
        config = load_config()
    except (FileNotFoundError, yaml.YAMLError) as exc:
        print(f"\n❌ Config error: {exc}")
        raise SystemExit(1) from None
    print(f"   {len(config['keywords'])} keywords, {len(config['research_authors'])} research authors, {len(config['colleagues']['people'])} colleagues, view mode: {config['recipient_view_mode']}")

    print("\n📡 Fetching papers from arXiv...")
    try:
        papers = fetch_all_papers(config)
    except urllib.error.URLError as exc:
        print(f"\n❌ arXiv fetch failed (network error): {exc}")
        print("   Check your internet connection, or try again — arXiv may be temporarily down.")
        raise SystemExit(1) from None
    except Exception as exc:
        print(f"\n❌ arXiv fetch failed: {exc}")
        print("   If this keeps happening, open an issue: https://github.com/SilkeDainese/arxiv-digest/issues")
        raise SystemExit(1) from None

    if not papers:
        print("\n⚠️  No papers fetched — all arXiv category requests failed or returned nothing.")
        print("   Skipping email to avoid sending an empty digest. Check the errors above.")
        raise SystemExit(1) from None

    print("\n👍 Ingesting quick-feedback votes...")
    feedback_stats = ingest_feedback_from_github(config)
    apply_feedback_bias(papers, feedback_stats)
    mirror_feedback_to_central(feedback_stats, config)

    # Track keyword performance
    print("\n📊 Updating keyword stats...")
    update_keyword_stats(papers, config)

    # Extract own papers and colleague papers before filtering (they always show)
    own_papers = extract_own_papers(papers)
    if own_papers:
        print(f"   🎉 YOU published {len(own_papers)} paper(s)! Congratulations!")

    colleague_papers = extract_colleague_papers(papers)
    if colleague_papers:
        names = set()
        for p in colleague_papers:
            names.update(p["colleague_matches"])
        print(f"   🎉 Found {len(colleague_papers)} colleague paper(s): {', '.join(names)}")

    print("\n🔍 Pre-filtering...")
    candidates = pre_filter(papers)

    print("\n🤖 Analysing papers...")
    final_papers, scoring_method = analyse_papers(candidates, config)
    print(f"   {len(final_papers)} papers made the cut (scoring: {scoring_method})")

    if not final_papers and not colleague_papers and not own_papers:
        print("\n⚠️  No papers made the cut this week (all scored below min_score threshold).")
        print(f"   Threshold: {config['min_score']}. Papers fetched: {len(papers)}.")
        print("   Sending digest anyway to confirm the pipeline is working.")

    print("\n🎨 Rendering HTML...")
    html = render_html(final_papers, colleague_papers, config, date_str, own_papers=own_papers, scoring_method=scoring_method)

    own_count = len(set(p["id"] for p in own_papers)) if own_papers else 0
    total_count = len(final_papers) + len(set(p["id"] for p in colleague_papers)) + own_count

    # Save HTML artifact (always)
    output_path = Path(__file__).parent / "digest_output.html"
    try:
        with open(output_path, "w") as f:
            f.write(html)
    except OSError as exc:
        print(f"  ⚠️  Could not save HTML artifact: {exc}")
        print("   Continuing with email delivery...")

    if preview_mode:
        print(f"\n👀 Preview saved to {output_path}")
        webbrowser.open(f"file://{output_path.resolve()}")
        print("   Opened in your browser. No email sent.")
    else:
        print("\n📧 Sending email...")
        try:
            if not send_email(html, total_count, date_str, config, papers=final_papers):
                print("\n❌ Email delivery failed.")
                raise SystemExit(1)
        except SystemExit:
            raise
        except Exception as exc:
            print(f"\n❌ Email send error: {exc}")
            print(f"   HTML saved to {output_path}")
            print("   If this keeps happening, open an issue: https://github.com/SilkeDainese/arxiv-digest/issues")
            raise SystemExit(1) from None

    print("\n✨ Done!\n")


if __name__ == "__main__":
    _config_for_failure: dict[str, Any] | None = None
    try:
        # Load config early so failure reports can reach the right inbox
        if CONFIG_PATH.exists() or CONFIG_EXAMPLE_PATH.exists():
            try:
                _config_for_failure = load_config()
            except Exception:
                pass
        main()
    except SystemExit:
        raise
    except Exception as _exc:
        import traceback
        _tb = traceback.format_exc()
        print(f"\n❌ Unhandled exception in digest pipeline:\n{_tb}", file=sys.stderr)
        try:
            send_failure_report(_config_for_failure, _tb)
        except Exception as _report_exc:
            print(f"⚠️  Could not send failure report: {_report_exc}", file=sys.stderr)
        raise SystemExit(1) from None
