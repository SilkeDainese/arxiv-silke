"""
arXiv Digest — Setup Wizard Backend
A lightweight Flask server that serves the setup wizard HTML and provides
API endpoints for ORCID lookup, AI suggestions, config generation, and
student registration.

Created by Silke S. Dainese · dainese@phys.au.dk
"""

from __future__ import annotations

import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml
from flask import Flask, jsonify, request, send_from_directory

# Allow imports from the project root (one level up from setup/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from data import ARXIV_CATEGORIES, CATEGORY_HINTS

try:
    import anthropic as _anthropic_lib

    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

try:
    from google import genai as _genai_lib
    from google.genai import types as _genai_types

    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False

try:
    from pure_scraper import (
        fetch_orcid_person,
        fetch_orcid_works,
        find_au_colleagues,
    )

    _PURE_AVAILABLE = True
except Exception:
    _PURE_AVAILABLE = False

# ─────────────────────────────────────────────────────────────
#  App setup
# ─────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder=None)

_ORCID_ID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")

DEFAULT_STUDENT_MANAGE_URL = os.environ.get(
    "STUDENT_MANAGE_URL",
    "https://arxiv-digest-relay.vercel.app/api/students",
).strip()

# Invite codes loaded from environment variable (JSON string)
# Format: {"code1": {"relay_token": "...", "gemini_api_key": "..."}, ...}
_INVITE_CODES: dict[str, dict[str, str]] = {}
_raw_codes = os.environ.get("INVITE_CODES_JSON", "").strip()
if _raw_codes:
    try:
        _INVITE_CODES = json.loads(_raw_codes)
    except json.JSONDecodeError:
        pass

# Rate limiting — not yet implemented; add Flask-Limiter when this moves to a shared deployment


# ─────────────────────────────────────────────────────────────
#  AI helpers
# ─────────────────────────────────────────────────────────────


def _call_ai(
    prompt: str,
    max_tokens: int = 512,
    gemini_key: str = "",
    anthropic_key: str = "",
) -> str | None:
    """Call Gemini (preferred) or Claude. Returns text or None on failure."""
    if gemini_key and _GEMINI_AVAILABLE:
        try:
            client = _genai_lib.Client(api_key=gemini_key)
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
            )
            return response.text.strip()
        except Exception:
            pass

    if anthropic_key and _ANTHROPIC_AVAILABLE:
        try:
            client = _anthropic_lib.Anthropic(api_key=anthropic_key)
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()
        except Exception:
            pass

    return None


def _test_ai_key(gemini_key: str, anthropic_key: str) -> tuple[bool, str, str]:
    """Validate that at least one AI key works. Returns (ok, provider, error)."""
    gemini_err = ""
    if gemini_key and _GEMINI_AVAILABLE:
        try:
            client = _genai_lib.Client(api_key=gemini_key)
            client.models.generate_content(
                model="gemini-2.0-flash",
                contents="Hi",
                config=_genai_types.GenerateContentConfig(max_output_tokens=1),
            )
            return True, "Gemini", ""
        except Exception as e:
            gemini_err = str(e)

    anthropic_err = ""
    if anthropic_key and _ANTHROPIC_AVAILABLE:
        try:
            client = _anthropic_lib.Anthropic(api_key=anthropic_key)
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1,
                messages=[{"role": "user", "content": "Hi"}],
            )
            return True, "Anthropic", ""
        except Exception as e:
            anthropic_err = str(e)

    errors = []
    if gemini_err:
        errors.append(f"Gemini: {gemini_err}")
    if anthropic_err:
        errors.append(f"Anthropic: {anthropic_err}")
    return False, "", " | ".join(errors) if errors else "No valid key entered."


# ─────────────────────────────────────────────────────────────
#  Keyword extraction (regex fallback — no API needed)
# ─────────────────────────────────────────────────────────────

_STOPWORDS = {
    "i", "my", "me", "we", "our", "the", "a", "an", "and", "or", "but",
    "in", "on", "at", "to", "for", "of", "with", "by", "from", "as",
    "is", "was", "are", "were", "been", "be", "have", "has", "had",
    "do", "does", "did", "will", "would", "could", "should", "that",
    "which", "who", "this", "these", "it", "its", "their", "also",
    "using", "such", "both", "between", "about", "into", "through",
    "particularly", "specifically", "especially", "including",
    "focus", "work", "study", "research", "currently", "mainly", "primarily",
}


def _keyword_regex_fallback(text: str) -> dict[str, int]:
    """Extract keywords from text using pattern matching."""
    words = text.split()
    candidates: dict[str, int] = {}
    clean_words = [re.sub(r"[.,;:!?()\"']", "", w) for w in words if w]

    for w in clean_words:
        if not w or len(w) < 3:
            continue
        if w.isupper() and len(w) >= 2 and w.isalpha():
            candidates[w] = 8
        elif w[0].isupper() and not w.isupper() and len(w) > 3:
            candidates[w.lower()] = 5

    for i in range(len(clean_words) - 1):
        w1, w2 = clean_words[i].lower(), clean_words[i + 1].lower()
        if w1 not in _STOPWORDS and w2 not in _STOPWORDS and len(w1) > 2 and len(w2) > 2:
            bigram = f"{w1} {w2}"
            if bigram not in candidates:
                candidates[bigram] = 7

    for i in range(len(clean_words) - 2):
        w1, w2, w3 = clean_words[i].lower(), clean_words[i + 1].lower(), clean_words[i + 2].lower()
        if all(w not in _STOPWORDS and len(w) > 2 for w in (w1, w2, w3)):
            trigram = f"{w1} {w2} {w3}"
            if len(trigram) > 10:
                candidates[trigram] = 9

    generic = {"et al", "ground based", "non linear"}
    return {
        k: v
        for k, v in sorted(candidates.items(), key=lambda x: -x[1])[:25]
        if k.lower() not in generic
    }


def _suggest_categories(text: str, gemini_key: str = "", anthropic_key: str = "") -> list[str]:
    """Return up to 6 relevant arXiv category codes."""
    if gemini_key or anthropic_key:
        cat_list = "\n".join(f"  {code}: {name}" for code, name in ARXIV_CATEGORIES.items())
        prompt = (
            f'A researcher describes their work as:\n"{text}"\n\n'
            f"Here is the full list of arXiv categories:\n{cat_list}\n\n"
            "Return ONLY a JSON array of the 4–6 most relevant category codes "
            '(e.g. ["cond-mat.supr-con", "cond-mat.mes-hall"]). '
            "Pick the best-matching sub-categories — never return a bare top-level code "
            "like 'cond-mat' or 'astro-ph' unless it appears exactly in the list above. "
            "No explanation, no other text."
        )
        raw = _call_ai(prompt, gemini_key=gemini_key, anthropic_key=anthropic_key)
        if raw:
            try:
                raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
                raw = re.sub(r"\n?```$", "", raw)
                cats = json.loads(raw)
                valid = [c for c in cats if c in ARXIV_CATEGORIES]
                if valid:
                    return valid[:6]
            except Exception:
                pass

    # Regex fallback
    text_lower = text.lower()
    scores = {}
    for cat, hints in CATEGORY_HINTS.items():
        if cat not in ARXIV_CATEGORIES:
            continue
        score = sum(1 for h in hints if h.lower() in text_lower)
        if score >= 2:
            scores[cat] = score
    return sorted(scores, key=scores.get, reverse=True)[:6]


def _suggest_keywords(
    text: str,
    orcid_keywords: dict | None = None,
    gemini_key: str = "",
    anthropic_key: str = "",
) -> dict[str, int]:
    """Score keywords by relevance using AI or regex fallback."""
    if not (gemini_key or anthropic_key):
        return _keyword_regex_fallback(text)

    regex_kws = _keyword_regex_fallback(text)
    all_candidates = dict(regex_kws)
    if orcid_keywords:
        all_candidates.update(orcid_keywords)

    candidate_list = "\n".join(f"- {kw}" for kw in all_candidates)
    prompt = (
        f'A researcher describes their work as:\n"{text}"\n\n'
        f"These are candidate keywords:\n{candidate_list}\n\n"
        "Score each keyword's relevance on a scale of 1–10. "
        "Prefer specific technical terms over generic words. "
        "Return at most 25 keywords. "
        "Return ONLY a JSON object mapping each keyword to its integer score."
    )

    raw = _call_ai(prompt, gemini_key=gemini_key, anthropic_key=anthropic_key)
    if not raw:
        return _keyword_regex_fallback(text)
    try:
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        scored: dict[str, int] = json.loads(raw)
        return dict(
            sorted(
                {k: max(1, min(10, int(v))) for k, v in scored.items()}.items(),
                key=lambda x: -x[1],
            )[:25]
        )
    except Exception:
        return _keyword_regex_fallback(text)


def _summarise_research(
    titles: list[str], gemini_key: str = "", anthropic_key: str = ""
) -> str:
    """Generate a research summary from publication titles."""
    sample = titles[:30]
    titles_block = "\n".join(f"- {t}" for t in sample)
    prompt = (
        "Here are publication titles from a researcher's ORCID profile:\n"
        f"{titles_block}\n\n"
        "Write a 3-4 sentence research description in first person (starting with 'I') "
        "that captures what this researcher works on. Be specific about methods, objects, "
        "or phenomena. Return only the description, no other text."
    )
    return _call_ai(prompt, max_tokens=200, gemini_key=gemini_key, anthropic_key=anthropic_key) or ""


def _draft_description(
    keywords: dict[str, int], gemini_key: str = "", anthropic_key: str = ""
) -> str:
    """Generate a research description from keywords."""
    top_keywords = [k for k, _ in sorted(keywords.items(), key=lambda x: -x[1])[:10]]
    prompt = (
        f"A researcher has these keywords:\n{', '.join(top_keywords)}\n\n"
        "Write a 3-4 sentence research description in first person (starting with 'I'). "
        "Be specific and technical. Return only the description."
    )
    result = _call_ai(prompt, max_tokens=200, gemini_key=gemini_key, anthropic_key=anthropic_key)
    return result if result else f"My research focuses on {', '.join(top_keywords[:5])}."


# ─────────────────────────────────────────────────────────────
#  Name match patterns
# ─────────────────────────────────────────────────────────────


def _name_match_patterns(full_name: str) -> list[str]:
    """Generate arXiv name match patterns from a full name."""
    parts = full_name.strip().split()
    if len(parts) < 2:
        return [full_name] if full_name.strip() else []
    first, last = parts[0], parts[-1]
    patterns = [f"{last}, {first[0]}", full_name]
    if len(parts) > 2:
        patterns.append(f"{last}, {parts[0][0]}. {parts[1][0]}.")
    return patterns


# ─────────────────────────────────────────────────────────────
#  Routes — static files
# ─────────────────────────────────────────────────────────────


@app.route("/")
def index():
    return send_from_directory(Path(__file__).parent, "index.html")


# ─────────────────────────────────────────────────────────────
#  Routes — ORCID
# ─────────────────────────────────────────────────────────────


@app.route("/api/orcid/lookup", methods=["POST"])
def orcid_lookup():
    """Look up ORCID profile: person info + publications + coauthors."""
    if not _PURE_AVAILABLE:
        return jsonify({"error": "ORCID lookup not available (missing dependencies)"}), 503

    data = request.get_json(force=True)
    orcid_id = data.get("orcid_id", "").strip()

    # Accept full URL or bare ID
    if "orcid.org/" in orcid_id:
        orcid_id = orcid_id.split("orcid.org/")[-1].strip().rstrip("/")
    if not _ORCID_ID_RE.match(orcid_id):
        return jsonify({"error": "Invalid ORCID ID format (expected 0000-0000-0000-0000)"}), 400

    # Fetch person
    name, institution, person_err = fetch_orcid_person(orcid_id)
    if person_err:
        return jsonify({"error": person_err}), 404

    # Fetch works
    keywords, titles, works_meta, coauthor_map, coauthor_counts, works_err = fetch_orcid_works(orcid_id)

    # Generate name match patterns
    self_match = _name_match_patterns(name) if name else []

    # Get AI keys from request (optional)
    gemini_key = data.get("gemini_key", "")
    anthropic_key = data.get("anthropic_key", "")

    # Generate research summary from titles
    research_description = ""
    if titles:
        research_description = _summarise_research(titles, gemini_key, anthropic_key)

    return jsonify({
        "name": name,
        "institution": institution,
        "paper_count": len(titles),
        "coauthor_count": len(coauthor_counts),
        "keywords": keywords,
        "titles": titles[:50],
        "coauthor_counts": dict(sorted(coauthor_counts.items(), key=lambda x: -x[1])[:20]),
        "self_match": self_match,
        "research_description": research_description,
    })


# ─────────────────────────────────────────────────────────────
#  Routes — AI
# ─────────────────────────────────────────────────────────────


@app.route("/api/ai/test-key", methods=["POST"])
def ai_test_key():
    """Validate an AI key."""
    data = request.get_json(force=True)
    ok, provider, error = _test_ai_key(
        data.get("gemini_key", ""),
        data.get("anthropic_key", ""),
    )
    return jsonify({"ok": ok, "provider": provider, "error": error})


@app.route("/api/ai/suggest", methods=["POST"])
def ai_suggest():
    """AI-powered suggestions: categories + keywords + description from research context."""
    data = request.get_json(force=True)
    text = data.get("research_description", "")
    orcid_keywords = data.get("orcid_keywords")
    gemini_key = data.get("gemini_key", "")
    anthropic_key = data.get("anthropic_key", "")

    if not text:
        return jsonify({"error": "research_description is required"}), 400

    categories = _suggest_categories(text, gemini_key, anthropic_key)
    keywords = _suggest_keywords(text, orcid_keywords, gemini_key, anthropic_key)

    return jsonify({
        "categories": categories,
        "keywords": keywords,
    })


@app.route("/api/ai/suggest-people", methods=["POST"])
def ai_suggest_people():
    """Filter coauthors by AU affiliation."""
    if not _PURE_AVAILABLE:
        return jsonify({"error": "Pure scraper not available"}), 503

    data = request.get_json(force=True)
    coauthor_map = data.get("coauthor_map", {})
    coauthor_counts = data.get("coauthor_counts", {})
    institution = data.get("institution", "")

    colleagues = find_au_colleagues(coauthor_map, coauthor_counts, institution)
    return jsonify({"colleagues": colleagues})


# ─────────────────────────────────────────────────────────────
#  Routes — Config generation
# ─────────────────────────────────────────────────────────────


@app.route("/api/config/generate", methods=["POST"])
def config_generate():
    """Build config.yaml from form data and return it."""
    data = request.get_json(force=True)

    schedule = data.get("schedule", "mon_wed_fri")
    send_hour = data.get("send_hour_utc", 7)
    digest_mode = data.get("digest_mode", "highlights")

    config = {
        "digest_name": data.get("digest_name") or "arXiv Digest",
        "researcher_name": data.get("researcher_name") or "Reader",
        "research_context": data.get("research_context", ""),
        "categories": data.get("categories") or ["astro-ph.EP"],
        "keywords": data.get("keywords") or {"example keyword": 5},
        "self_match": data.get("self_match", []),
        "research_authors": data.get("research_authors", []),
        "colleagues": {
            "people": data.get("colleagues_people", []),
            "institutions": data.get("colleagues_institutions", []),
        },
        "digest_mode": digest_mode,
        "recipient_view_mode": data.get("recipient_view_mode", "deep_read"),
        "days_back": data.get("days_back", 4),
        "schedule": schedule,
        "send_hour_utc": send_hour,
        "institution": data.get("institution", ""),
        "department": data.get("department", ""),
        "tagline": data.get("tagline", ""),
        "smtp_server": data.get("smtp_server", "smtp.gmail.com"),
        "smtp_port": data.get("smtp_port", 587),
        "github_repo": data.get("github_repo", ""),
    }

    if data.get("max_papers"):
        config["max_papers"] = data["max_papers"]
    if data.get("min_score"):
        config["min_score"] = data["min_score"]

    config_yaml = yaml.dump(config, default_flow_style=False, sort_keys=False, allow_unicode=True)

    # Build cron expression
    cron_map = {
        "mon_wed_fri": f"0 {send_hour} * * 1,3,5",
        "weekdays": f"0 {send_hour} * * 1-5",
        "weekly": f"0 {send_hour} * * 1",
    }
    cron_expr = cron_map.get(schedule, f"0 {send_hour} * * 1,3,5")

    return jsonify({
        "config_yaml": config_yaml,
        "cron_expr": cron_expr,
    })


# ─────────────────────────────────────────────────────────────
#  Routes — Config parse (upload)
# ─────────────────────────────────────────────────────────────


@app.route("/api/config/parse", methods=["POST"])
def config_parse():
    """Parse an uploaded config.yaml and return form-friendly fields."""
    data = request.get_json(force=True)
    raw = data.get("yaml", "")
    if not raw.strip():
        return jsonify({"error": "Empty file"}), 400
    try:
        cfg = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return jsonify({"error": f"Invalid YAML: {exc}"}), 400
    if not isinstance(cfg, dict):
        return jsonify({"error": "Not a valid config.yaml (expected a mapping at the top level)"}), 400

    colleagues = cfg.get("colleagues", {})
    colleagues_people: list[str] = []
    if isinstance(colleagues, dict):
        colleagues_people = colleagues.get("people", []) or []
    elif isinstance(colleagues, list):
        colleagues_people = colleagues

    return jsonify({
        "researcher_name": cfg.get("researcher_name", ""),
        "research_context": cfg.get("research_context", ""),
        "categories": cfg.get("categories") or [],
        "keywords": cfg.get("keywords") or {},
        "colleagues_people": colleagues_people,
        "self_match": cfg.get("self_match") or [],
        "digest_mode": cfg.get("digest_mode", "highlights"),
        "recipient_view_mode": cfg.get("recipient_view_mode", "deep_read"),
        "schedule": cfg.get("schedule", "mon_wed_fri"),
        "digest_name": cfg.get("digest_name", ""),
        "institution": cfg.get("institution", ""),
        "tagline": cfg.get("tagline", ""),
    })


# ─────────────────────────────────────────────────────────────
#  Routes — Invite codes
# ─────────────────────────────────────────────────────────────


@app.route("/api/invite/validate", methods=["POST"])
def invite_validate():
    """Check an invite code and return unlocked keys."""
    data = request.get_json(force=True)
    code = data.get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "error": "No code provided"})

    for candidate, bundle in _INVITE_CODES.items():
        if hmac.compare_digest(code, candidate.strip()):
            unlocked = []
            if bundle.get("relay_token"):
                unlocked.append("Relay")
            if bundle.get("gemini_api_key"):
                unlocked.append("Gemini")
            if bundle.get("anthropic_api_key"):
                unlocked.append("Anthropic")
            return jsonify({
                "ok": True,
                "unlocked": unlocked,
                "gemini_key": bundle.get("gemini_api_key", ""),
                "anthropic_key": bundle.get("anthropic_api_key", ""),
                "relay_token": bundle.get("relay_token", ""),
            })

    return jsonify({"ok": False, "error": "Not recognised"})


# ─────────────────────────────────────────────────────────────
#  Routes — AU Student registration
# ─────────────────────────────────────────────────────────────


@app.route("/api/students/register", methods=["POST"])
def students_register():
    """Forward student subscription to the relay endpoint."""
    data = request.get_json(force=True)

    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    package_ids = data.get("package_ids", [])
    max_papers = data.get("max_papers_per_week", 6)

    if ' ' in email:
        return jsonify({"error": "Invalid email address"}), 400
    if not email.endswith("@uni.au.dk"):
        return jsonify({"error": "Only @uni.au.dk emails accepted"}), 400
    if len(password) < 4:
        return jsonify({"error": "Password too short"}), 400
    if not package_ids:
        return jsonify({"error": "Select at least one package"}), 400

    payload = {
        "action": "upsert",
        "email": email,
        "password": password,
        "new_password": "",
        "package_ids": package_ids,
        "max_papers_per_week": int(max_papers),
    }

    req = urllib.request.Request(
        DEFAULT_STUDENT_MANAGE_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
            return jsonify(result)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        try:
            error_payload = json.loads(body)
        except json.JSONDecodeError:
            error_payload = {}
        message = str(error_payload.get("error") or body or exc.reason)
        return jsonify({"error": message}), exc.code
    except urllib.error.URLError as exc:
        return jsonify({"error": f"Could not reach relay: {exc.reason}"}), 502


# ─────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"arXiv Digest Setup Wizard running on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("DEBUG", "").lower() == "true")
