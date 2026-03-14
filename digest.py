"""
arXiv Digest — Personalised paper curation engine.
Fetches new arXiv papers, scores them with AI (Claude → Gemini → keyword fallback),
and sends a beautiful HTML digest via email.

Configuration lives in config.yaml — edit that file to update keywords, colleagues, etc.
Use the setup wizard at arxiv-digest-setup.streamlit.app to generate your config.

Created by Silke S. Dainese · dainese@phys.au.dk · silkedainese.github.io
"""
from __future__ import annotations

import os
import json
import smtplib
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import yaml

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

try:
    import google.generativeai as genai
    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False

# ─────────────────────────────────────────────────────────────
#  CONFIGURATION (loaded from config.yaml)
# ─────────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "config.yaml"
CONFIG_EXAMPLE_PATH = Path(__file__).parent / "config.example.yaml"
STATS_PATH = Path(__file__).parent / "keyword_stats.json"


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
    cfg.setdefault("digest_mode", "highlights")  # "highlights" or "in_depth"
    cfg.setdefault("self_match", [])  # patterns to match YOUR name in author lists

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
    if isinstance(cfg["keywords"], list):
        cfg["keywords"] = {kw: 5 for kw in cfg["keywords"]}

    # ── Backward compat: flat colleagues list → people/institutions ──
    if isinstance(cfg["colleagues"], list):
        cfg["colleagues"] = {"people": cfg["colleagues"], "institutions": []}
    elif isinstance(cfg["colleagues"], dict):
        cfg["colleagues"].setdefault("people", [])
        cfg["colleagues"].setdefault("institutions", [])

    # ── Environment overrides (env var wins, config.yaml as fallback) ──
    cfg["recipient_email"] = os.environ.get("RECIPIENT_EMAIL", "").strip() or cfg.get("recipient_email", "")
    return cfg


# ─────────────────────────────────────────────────────────────
#  KEYWORD TRACKING
# ─────────────────────────────────────────────────────────────

def load_keyword_stats() -> dict[str, Any]:
    """Load keyword hit statistics from disk, or return empty dict if none exist."""
    if STATS_PATH.exists():
        with open(STATS_PATH) as f:
            return json.load(f)
    return {}


def save_keyword_stats(stats: dict[str, Any]) -> None:
    """Persist keyword hit statistics to disk as JSON."""
    with open(STATS_PATH, "w") as f:
        json.dump(stats, f, indent=2)


def update_keyword_stats(papers: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    """Track which keywords matched papers in this run."""
    stats = load_keyword_stats()
    today = datetime.utcnow().strftime("%Y-%m-%d")

    for kw in config["keywords"]:
        if kw not in stats:
            stats[kw] = {"total_hits": 0, "last_hit": None, "runs_checked": 0}
        stats[kw]["runs_checked"] += 1

    for paper in papers:
        text_lower = (paper["title"] + " " + paper["abstract"]).lower()
        for kw in config["keywords"]:
            if kw.lower() in text_lower:
                stats[kw]["total_hits"] += 1
                stats[kw]["last_hit"] = today

    save_keyword_stats(stats)

    # Report dormant keywords (no hits in 20+ runs)
    dormant = [kw for kw, s in stats.items()
               if s["runs_checked"] >= 20 and s["total_hits"] == 0]
    if dormant:
        print(f"  💤 Dormant keywords (0 hits in 20+ runs): {', '.join(dormant)}")

    return stats


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
        url = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
        print(f"  Fetching {category}...")

        req = urllib.request.Request(url)
        req.add_header("User-Agent", "arxiv-digest/1.0 (GitHub Actions; https://github.com/SilkeDainese/arxiv-digest)")

        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                xml_data = response.read().decode("utf-8")
        except Exception as e:
            print(f"  Error: {e}")
            continue

        root = ET.fromstring(xml_data)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        cutoff = datetime.utcnow() - timedelta(days=config["days_back"])

        for entry in root.findall("atom:entry", ns):
            try:
                published_str = entry.find("atom:published", ns).text
                published = datetime.fromisoformat(published_str.replace("Z", "+00:00")).replace(tzinfo=None)
                if published < cutoff:
                    continue

                arxiv_id = entry.find("atom:id", ns).text.split("/abs/")[-1]
                title = (entry.find("atom:title", ns).text or "").strip().replace("\n", " ")
                abstract = (entry.find("atom:summary", ns).text or "").strip().replace("\n", " ")
                authors = [a.find("atom:name", ns).text for a in entry.findall("atom:author", ns) if a.find("atom:name", ns) is not None and a.find("atom:name", ns).text]
            except (AttributeError, TypeError, ValueError):
                continue  # skip malformed entries

            # Check research authors (relevance boost)
            known_flag = []
            for author in authors:
                for known in config["research_authors"]:
                    if known.lower() in author.lower():
                        known_flag.append(author)
                        break

            # Check colleagues — people matches
            colleague_flag = []
            for author in authors:
                for colleague in config["colleagues"]["people"]:
                    for pattern in colleague.get("match", []):
                        if pattern.lower() in author.lower():
                            colleague_flag.append(colleague["name"])
                            break

            # Check colleagues — institutional matches (arXiv affiliation XML + abstract fallback)
            affiliations = []
            ns_arxiv = {"arxiv": "http://arxiv.org/schemas/atom"}
            for author_el in entry.findall("atom:author", ns):
                for aff_el in author_el.findall("arxiv:affiliation", ns_arxiv):
                    aff_text = aff_el.text
                    if aff_text:
                        affiliations.append(aff_text)
            affiliation_text = " ".join(affiliations).lower()
            text_lower = (title + " " + abstract).lower()
            for inst in config["colleagues"].get("institutions", []):
                inst_lower = inst.lower()
                if inst_lower in affiliation_text or inst_lower in text_lower:
                    colleague_flag.append(inst)

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
            kw_hits_raw = sum(
                weight for kw, weight in config["keywords"].items()
                if kw.lower() in text_lower
            )

            papers.append({
                "id": arxiv_id,
                "title": title,
                "abstract": abstract,
                "authors": authors,
                "published": published.strftime("%Y-%m-%d"),
                "category": category,
                "url": f"https://arxiv.org/abs/{arxiv_id}",
                "known_authors": known_flag,
                "colleague_matches": colleague_flag,
                "is_own_paper": is_own_paper,
                "keyword_hits_raw": kw_hits_raw,
            })

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


def pre_filter(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep papers that match keywords or have known authors."""
    filtered = [p for p in papers if p["keyword_hits"] > 0 or p["known_authors"]]
    filtered.sort(key=lambda p: (len(p["known_authors"]) * 15 + p["keyword_hits"]), reverse=True)
    if filtered:
        print(f"   {len(filtered)} matched keywords/authors (sending top 30 to AI)")
        return filtered[:30]
    # Discovery mode: no keyword/author matches — return newest papers
    print("  No keyword matches — discovery mode: showing newest papers")
    discovery = sorted(papers, key=lambda p: p.get("published", ""), reverse=True)
    return discovery[:30]


def extract_colleague_papers(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Separate out papers by colleagues — these always show regardless of score."""
    return [p for p in papers if p.get("colleague_matches")]


def extract_own_papers(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Separate out the user's own papers — these get a celebration section."""
    return [p for p in papers if p.get("is_own_paper")]


# ─────────────────────────────────────────────────────────────
#  AI ANALYSIS — Claude → Gemini → Keyword Fallback
# ─────────────────────────────────────────────────────────────

def _build_scoring_prompt(paper: dict[str, Any], config: dict[str, Any]) -> str:
    """Shared prompt builder used by both Claude and Gemini paths."""
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

Respond with ONLY a valid JSON object (no markdown, no backticks):
{{
  "relevance_score": <integer 1-10>,
  "plain_summary": "<2-3 sentences explaining what they did, like explaining to a smart friend at a pub>",
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
    return {
        "relevance_score": min(10, max(1, round(paper["keyword_hits"] / 10) + len(paper["known_authors"]) * 3)) if paper["keyword_hits"] > 0 or paper["known_authors"] else 1,
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
                model=config.get("claude_model", "claude-sonnet-4-20250514"),
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}]
            )
            analysis = json.loads(response.content[0].text.strip())
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


def _analyse_with_gemini(papers: list[dict[str, Any]], config: dict[str, Any], api_key: str) -> list[dict[str, Any]]:
    """Score papers using Gemini 2.0 Flash (free tier)."""
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")
    analysed = []

    for i, paper in enumerate(papers):
        print(f"  Analysing {i+1}/{len(papers)} (Gemini): {paper['title'][:60]}...")
        prompt = _build_scoring_prompt(paper, config)

        if i > 0:
            time.sleep(4)  # free tier = 15 RPM

        try:
            response = model.generate_content(prompt)
            text = response.text.strip()
            # Strip markdown code fences if present
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                text = text.strip()
                if text.endswith("```"):
                    text = text[:-3].strip()
            analysis = json.loads(text)
            paper.update(analysis)
            analysed.append(paper)
            print(f"    → score: {analysis.get('relevance_score', '?')}")
        except Exception as e:
            print(f"    Error: {e}")
            paper.update(_default_analysis(paper))
            analysed.append(paper)

    return _filter_and_sort(analysed, config)


def _fallback_analyse(papers: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Keyword-only scoring when no API key is available."""
    discovery_mode = not config["keywords"]
    for p in papers:
        if discovery_mode:
            # Discovery mode: score by proxies for significance
            author_score = min(5, len(p["authors"]) // 3)
            known_boost = len(p["known_authors"]) * 3
            score = min(10, max(1, author_score + known_boost + 2))
            why = "Discovery mode — scored by team size and author matches."
        else:
            # keyword_hits is normalized 0-100, map to 1-10 relevance
            score = min(10, max(1, round(p["keyword_hits"] / 10) + len(p["known_authors"]) * 3))
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
    result.sort(key=lambda p: p.get("relevance_score", 0), reverse=True)

    dropped = [p for p in analysed if p.get("relevance_score", 0) < config["min_score"]]
    if dropped:
        print(f"   Dropped {len(dropped)} papers below score {config['min_score']}:")
        for p in dropped:
            print(f"     {p.get('relevance_score', 0)}/10 — {p['title'][:60]}")

    return result[:config["max_papers"]]


def analyse_papers(papers: list[dict[str, Any]], config: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Dispatch to the best available AI backend, with cascade on failure.
    Returns (scored_papers, scoring_method) where scoring_method is one of:
    'claude', 'gemini', 'keywords', or 'keywords_fallback'."""
    if not papers:
        return [], "none"

    api_key_claude = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    api_key_gemini = os.environ.get("GEMINI_API_KEY", "").strip()

    # Try Claude first
    if api_key_claude and HAS_ANTHROPIC:
        print("  Using Claude for analysis...")
        result, error = _analyse_with_claude(papers, config, api_key_claude)
        if error is None:
            return result, "claude"
        # Claude failed — cascade to Gemini or keywords
        if api_key_gemini and HAS_GEMINI:
            print("  Cascading to Gemini 2.0 Flash...")
            return _analyse_with_gemini(papers, config, api_key_gemini), "gemini"
        else:
            print("  No Gemini key available — falling back to keyword-only scoring")
            return _fallback_analyse(papers, config), "keywords_fallback"

    # No Claude key — try Gemini
    if api_key_gemini and HAS_GEMINI:
        print("  Using Gemini 2.0 Flash for analysis...")
        return _analyse_with_gemini(papers, config, api_key_gemini), "gemini"

    # No AI keys at all
    print("  ⚠️  No AI API key set — using keyword-only scoring")
    return _fallback_analyse(papers, config), "keywords"


# ─────────────────────────────────────────────────────────────
#  HTML RENDERING  (email-safe: inline styles + table layout)
# ─────────────────────────────────────────────────────────────

# ── Brand palette ──
from brand import (PINE, GOLD, UMBER, ASH_WHITE, ASH_BLACK,
                   CARD_BORDER, WARM_GREY, PINE_WASH, PINE_LIGHT, GOLD_LIGHT)


# ── Shared inline-style constants ──
_TAG = f"font-family:'DM Mono',monospace;font-size:10px;letter-spacing:0.1em;text-transform:uppercase;padding:2px 8px;border-radius:3px;display:inline-block;margin:2px 3px 2px 0;color:{WARM_GREY}"


def _score_bar(score: int | float) -> str:
    """Return a 10-dot bar visualising the relevance score."""
    filled = round(score)
    return "".join(["●" if i < filled else "○" for i in range(10)])


def _accent_color(score: int | float) -> str:
    """Map a relevance score to a brand accent colour."""
    if score >= 9: return PINE
    if score >= 7: return PINE_LIGHT
    if score >= 5: return GOLD
    return UMBER


def _build_tags(p: dict[str, Any]) -> str:
    """Build inline HTML tag spans for a paper card."""
    score = p.get("relevance_score", 5)
    tags = []
    tags.append(f'<span style="{_TAG};background:{PINE_WASH};color:{PINE}">{p["category"]}</span>')
    tags.append(f'<span style="{_TAG};color:{WARM_GREY}">{p["published"]}</span>')
    for a in p.get("known_authors", []):
        tags.append(f'<span style="{_TAG};background:#FFF8E1;color:{UMBER}">&#x1F44B; {a}</span>')
    if score >= 9:
        tags.append(f'<span style="{_TAG};background:#FFF0F0;color:#C0392B">&#x1F525; must-read</span>')
    elif score >= 8:
        tags.append(f'<span style="{_TAG};background:#FFF8E1;color:{UMBER}">&#x1F4CC; thesis</span>')
    for kw in (p.get("kw_tags") or [])[:2]:
        tags.append(f'<span style="{_TAG};background:{PINE_WASH};color:{PINE}">{kw}</span>')
    if p.get("is_new_catalog"):
        tags.append(f'<span style="{_TAG};background:#F3E8FF;color:#6B21A8">&#x1F4E6; catalog</span>')
    if p.get("cite_worthy"):
        tags.append(f'<span style="{_TAG};background:{PINE_WASH};color:{PINE}">&#x1F4CE; cite this</span>')
    if p.get("new_result"):
        tags.append(f'<span style="{_TAG};background:{PINE_WASH};color:{PINE}">{p["new_result"]}</span>')
    return " ".join(tags)


def _build_method_tags(p: dict[str, Any]) -> str:
    """Build inline HTML method tag spans for a paper card."""
    return " ".join(f'<span style="{_TAG};background:#FFF8E1;color:{UMBER}">{t}</span>' for t in (p.get("method_tags") or []))


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
          <tr><td style="background:linear-gradient(135deg, {PINE_WASH}, #FFF8E1);border:2px solid {GOLD};border-radius:8px;padding:20px 22px">
            <div style="font-family:'DM Mono',monospace;font-size:10px;letter-spacing:0.2em;text-transform:uppercase;color:{PINE};margin-bottom:8px">&#x1F389; Congratulations, {researcher_name}!</div>
            <div style="font-family:'DM Serif Display',Georgia,serif;font-size:18px;color:{ASH_BLACK};line-height:1.4;margin-bottom:6px">
              <a href="{p['url']}" style="color:{ASH_BLACK};text-decoration:none">{p['title']}</a>
            </div>
            <div style="font-family:'DM Mono',monospace;font-size:10px;color:{WARM_GREY};margin-bottom:10px">{authors_short}</div>
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
        names = ", ".join(set(p["colleague_matches"]))
        authors_short = ", ".join(p["authors"][:3])
        if len(p["authors"]) > 3:
            authors_short += f" +{len(p['authors'])-3}"
        postits += f"""
        <table width="48%" cellpadding="0" cellspacing="0" border="0" style="display:inline-table;vertical-align:top;margin:6px 1%">
          <tr><td style="background:#FFF8E1;border:1px solid {GOLD_LIGHT};border-radius:6px;padding:14px 16px">
            <div style="font-family:'DM Mono',monospace;font-size:9px;letter-spacing:0.15em;text-transform:uppercase;color:{UMBER};margin-bottom:6px">&#x1F389; {names}</div>
            <div style="font-family:'IBM Plex Sans',sans-serif;font-size:13px;color:{ASH_BLACK};line-height:1.4;margin-bottom:4px">
              <a href="{p['url']}" style="color:{ASH_BLACK};text-decoration:none">{p['title'][:80]}{'...' if len(p['title']) > 80 else ''}</a>
            </div>
            <div style="font-family:'DM Mono',monospace;font-size:10px;color:{WARM_GREY}">{authors_short}</div>
          </td></tr>
        </table>"""

    return f"""
  <!-- COLLEAGUE NEWS -->
  <tr><td style="padding:20px 44px 8px;font-family:'DM Mono',monospace;font-size:9px;letter-spacing:0.25em;text-transform:uppercase;color:{UMBER}">&#x2500;&#x2500; Colleague news &#x1F4EC; &#x2500;&#x2500;</td></tr>
  <tr><td style="padding:4px 24px 16px">
    <div style="font-family:'IBM Plex Sans',sans-serif;font-size:12px;color:{WARM_GREY};font-style:italic;margin-bottom:10px">Papers by people you know — send congrats!</div>
    {postits}
  </td></tr>"""


def _render_paper_card(p: dict[str, Any], is_top_pick: bool, total_papers: int) -> str:
    """Return the HTML for a single paper card."""
    score = p.get("relevance_score", 5)
    ac = _accent_color(score)
    authors_display = ", ".join(p["authors"][:5])
    if len(p["authors"]) > 5:
        authors_display += f" +{len(p['authors'])-5} more"
    top_label = f'<span style="font-family:\'DM Mono\',monospace;font-size:9px;letter-spacing:0.2em;text-transform:uppercase;background:{PINE};color:white;padding:3px 12px;display:inline-block;border-radius:3px;margin-bottom:12px">&#x2B51; Top pick</span>' if is_top_pick and total_papers > 1 else ''
    method_html = _build_method_tags(p)
    footer_methods = f'<div style="margin-top:12px">{method_html}</div>' if method_html else ''

    return f"""
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:16px">
      <tr><td style="background:white;border:1px solid {CARD_BORDER};border-left:4px solid {ac};border-radius:8px;padding:24px 26px 20px">
        {top_label}
        <!-- Header: tags + score -->
        <table width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr>
            <td style="vertical-align:top;padding-bottom:10px">{_build_tags(p)}</td>
            <td width="80" style="vertical-align:top;text-align:right;padding-bottom:10px">
              <span style="font-size:28px;line-height:1">{p.get('emoji','&#x1F52D;')}</span><br>
              <span style="font-family:'DM Serif Display',Georgia,serif;font-size:26px;color:{ac};line-height:1">{score}</span><span style="font-size:13px;color:{WARM_GREY}">/10</span><br>
              <span style="font-family:'DM Mono',monospace;font-size:8px;letter-spacing:2px;color:{ac};opacity:0.55">{_score_bar(score)}</span>
            </td>
          </tr>
        </table>
        <!-- Title block -->
        <div style="font-family:'DM Serif Display',Georgia,serif;font-size:14px;font-style:italic;color:{WARM_GREY};margin-bottom:6px">{p.get('highlight_phrase','')}</div>
        <div style="font-family:'IBM Plex Sans',sans-serif;font-size:16px;font-weight:bold;color:{ASH_BLACK};line-height:1.4;margin-bottom:5px"><a href="{p['url']}" style="color:{ASH_BLACK};text-decoration:none">{p['title']}</a></div>
        <div style="font-family:'DM Mono',monospace;font-size:10px;color:{WARM_GREY};margin-bottom:16px">{authors_display}</div>
        <!-- Summaries -->
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:12px">
          <tr><td style="background:{ASH_WHITE};border-radius:5px;padding:12px 14px">
            <div style="font-family:'DM Mono',monospace;font-size:9px;letter-spacing:0.2em;text-transform:uppercase;color:{WARM_GREY};margin-bottom:6px">&#x1F9EA; What they did</div>
            <div style="font-family:'IBM Plex Sans',sans-serif;font-size:13px;color:{ASH_BLACK};line-height:1.75">{p.get('plain_summary','')}</div>
          </td></tr>
        </table>
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:12px">
          <tr><td style="background:#FFF8E1;border:1px solid {GOLD_LIGHT};border-radius:5px;padding:12px 14px">
            <div style="font-family:'DM Mono',monospace;font-size:9px;letter-spacing:0.2em;text-transform:uppercase;color:{WARM_GREY};margin-bottom:6px">&#x2B50; Why it matters to you</div>
            <div style="font-family:'IBM Plex Sans',sans-serif;font-size:13px;color:{UMBER};line-height:1.75">{p.get('why_interesting','')}</div>
          </td></tr>
        </table>
        <!-- Footer -->
        <table width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr>
            <td style="vertical-align:middle">{footer_methods}</td>
            <td width="120" style="text-align:right;vertical-align:middle">
              <a href="{p['url']}" style="font-family:'DM Mono',monospace;font-size:10px;letter-spacing:0.1em;text-transform:uppercase;color:{PINE};text-decoration:none;border:1px solid {PINE};padding:6px 14px;border-radius:3px;display:inline-block">Read paper &#x2192;</a>
            </td>
          </tr>
        </table>
      </td></tr>
    </table>"""


def _render_scoring_notice(scoring_method: str) -> str:
    """Return the scoring-method notice banner HTML (or empty string)."""
    if scoring_method == "keywords_fallback":
        return f"""
  <tr><td style="padding:12px 44px">
    <div style="background:#FFF8E1;border:1px solid {GOLD_LIGHT};border-radius:6px;padding:14px 18px;font-family:'IBM Plex Sans',sans-serif;font-size:12px;color:{UMBER};text-align:center">
      &#x26A0;&#xFE0F; <strong>AI scoring unavailable this run</strong> — your API key may be out of credits. Papers were scored by keyword matching only, so relevance scores may be less accurate. Top up at <a href="https://console.anthropic.com" style="color:{PINE}">console.anthropic.com</a> or add a free <a href="https://aistudio.google.com/apikey" style="color:{PINE}">Gemini API key</a> as backup.
    </div>
  </td></tr>"""
    elif scoring_method == "keywords":
        return f"""
  <tr><td style="padding:12px 44px">
    <div style="background:{PINE_WASH};border:1px solid {CARD_BORDER};border-radius:6px;padding:14px 18px;font-family:'IBM Plex Sans',sans-serif;font-size:12px;color:{WARM_GREY};text-align:center">
      &#x1F4CA; Papers scored by keyword matching (no AI key configured). For smarter scoring, add an <code>ANTHROPIC_API_KEY</code> ($5 of credits will last hundreds of digests) or a free <code>GEMINI_API_KEY</code> to your repo secrets.
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
    setup_url = "https://arxiv-digest-setup.streamlit.app"
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
    else:
        service_links.append(f'<a href="{setup_url}" style="{link_style}">&#x2699;&#xFE0F; Edit preferences</a>')

    self_service_html = f"""
    <div style="font-family:'DM Mono',monospace;font-size:10px;color:{WARM_GREY};letter-spacing:0.08em;margin-bottom:14px;text-align:center">
      {' &middot; '.join(service_links)}
    </div>"""

    # ── Scoring label ──
    scoring_labels = {
        "claude": "Claude (Anthropic)",
        "gemini": "Gemini 2.0 Flash (Google)",
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
  </td></tr>"""


def _render_css(digest_name: str, researcher_name: str, date_str: str) -> str:
    """Return the HTML head with font imports and opening body/table tags."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{digest_name}{'' if researcher_name.split()[0].lower() in digest_name.lower() else f' for {researcher_name}'} — {date_str}</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=IBM+Plex+Sans:wght@300;400;600&family=DM+Mono:wght@400&display=swap" rel="stylesheet">
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

    if own_papers is None:
        own_papers = []

    # ── Build paper cards ──
    cards_html = ""
    for i, p in enumerate(papers):
        cards_html += _render_paper_card(p, is_top_pick=(i == 0), total_papers=len(papers))

    if not papers and not colleague_papers:
        cards_html = f'<div style="text-align:center;padding:60px 24px;color:{WARM_GREY};font-family:\'DM Serif Display\',Georgia,serif;font-style:italic;font-size:18px">No highly relevant papers this period. The cosmos is quiet. &#x2615;</div>'

    # ── Assemble full document ──
    return (
        _render_css(digest_name, researcher_name, date_str)
        + "\n"
        + _render_header(papers, colleague_papers, config, date_str, researcher_name, digest_name)
        + "\n"
        + f"""
  {_render_own_paper_section(own_papers, researcher_name)}

  {_render_colleague_section(colleague_papers)}

  <!-- SECTION DIVIDER -->
  <tr><td style="padding:20px 44px 14px;font-family:'DM Mono',monospace;font-size:9px;letter-spacing:0.25em;text-transform:uppercase;color:{WARM_GREY}">&#x2500;&#x2500; All papers this edition &middot; {len(papers)} {"paper" if len(papers) == 1 else "papers"} &#x2500;&#x2500;</td></tr>

  <!-- PAPER CARDS -->
  <tr><td style="padding:0 24px 52px">
    {cards_html}
  </td></tr>

  {_render_scoring_notice(scoring_method)}
"""
        + _render_footer(config, scoring_method)
        + """

</table>
</td></tr>
</table>
</body>
</html>"""
    )


# ─────────────────────────────────────────────────────────────
#  EMAIL SENDING — Multi-provider SMTP
# ─────────────────────────────────────────────────────────────

def send_email(html: str, paper_count: int, date_str: str, config: dict[str, Any]) -> None:
    """Send the digest HTML as an email via SMTP.

    Args:
        html: Rendered HTML body of the digest.
        paper_count: Total number of papers to display in the subject line.
        date_str: Human-readable date string for the subject line.
        config: Application config containing SMTP and recipient settings.
    """
    recipient = config["recipient_email"]

    # Support both new (SMTP_*) and legacy (GMAIL_*) secret names
    smtp_user = os.environ.get("SMTP_USER", "").strip() or os.environ.get("GMAIL_USER", "").strip()
    smtp_password = os.environ.get("SMTP_PASSWORD", "").strip() or os.environ.get("GMAIL_APP_PASSWORD", "").strip()

    if not all([recipient, smtp_user, smtp_password]):
        print("⚠️  SMTP credentials or RECIPIENT_EMAIL not set — skipping email send.")
        return

    smtp_server = config["smtp_server"]
    smtp_port = config["smtp_port"]

    digest_name = config["digest_name"]
    researcher_name = config["researcher_name"]
    paper_word = "paper" if paper_count == 1 else "papers"
    subject = f"🔭 {digest_name} — {paper_count} {paper_word} · {date_str}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{digest_name} <{smtp_user}>"
    msg["To"] = recipient
    msg.attach(MIMEText(f"Your arXiv digest for {date_str} — {paper_count} papers. Open in a browser for the full experience.", "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, [recipient], msg.as_string())
        print(f"✅ Email sent to {recipient} via {smtp_server}")
    except smtplib.SMTPAuthenticationError as e:
        print(f"❌ SMTP auth failed: {e}")
        if "gmail" in smtp_server.lower():
            print("   Make sure SMTP_PASSWORD is a Gmail App Password, not your regular password.")
            print("   Generate one at: Google Account > Security > 2-Step Verification > App passwords")
        elif "office365" in smtp_server.lower():
            print("   For Office 365, use an App Password from your Microsoft account security settings.")
    except Exception as e:
        print(f"❌ Email send failed: {e}")
        print("📋 Digest was saved as digest_output.html artifact — check Actions artifacts to download it.")


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────

def main() -> None:
    """Run the full arXiv digest pipeline: fetch, score, render, and email."""
    import sys
    import webbrowser

    preview_mode = "--preview" in sys.argv

    date_str = datetime.utcnow().strftime("%B %d, %Y")
    print(f"\n🔭 arXiv Digest — {date_str}")
    if preview_mode:
        print("   (preview mode — no email will be sent)")
    print("=" * 50)

    print("\n📋 Loading config.yaml...")
    config = load_config()
    print(f"   {len(config['keywords'])} keywords, {len(config['research_authors'])} research authors, {len(config['colleagues']['people'])} colleagues")

    print("\n📡 Fetching papers from arXiv...")
    papers = fetch_arxiv_papers(config)

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

    print("\n🎨 Rendering HTML...")
    html = render_html(final_papers, colleague_papers, config, date_str, own_papers=own_papers, scoring_method=scoring_method)

    own_count = len(set(p["id"] for p in own_papers)) if own_papers else 0
    total_count = len(final_papers) + len(set(p["id"] for p in colleague_papers)) + own_count

    # Save HTML artifact (always)
    output_path = Path(__file__).parent / "digest_output.html"
    with open(output_path, "w") as f:
        f.write(html)

    if preview_mode:
        print(f"\n👀 Preview saved to {output_path}")
        webbrowser.open(f"file://{output_path.resolve()}")
        print("   Opened in your browser. No email sent.")
    else:
        print("\n📧 Sending email...")
        send_email(html, total_count, date_str, config)

    print("\n✨ Done!\n")


if __name__ == "__main__":
    main()
