"""
arXiv Digest — Setup Wizard
A Streamlit web app that helps researchers configure their personal arXiv digest.
Generates a config.yaml (+ workflow snippet) ready to use with the arxiv-digest template.

Created by Silke S. Dainese · dainese@phys.au.dk · silkedainese.github.io
"""

import json
import hmac
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

import yaml
import streamlit as st

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

# Allow imports from the project root (one level up from setup/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brand import PINE, GOLD, CARD_BORDER, WARM_GREY
from data import (
    ASTRO_MINI_TRACKS,
    AU_STUDENT_TRACK_LABELS,
    AU_STUDENT_ALWAYS_TAG,
    AU_ASTRONOMY_PEOPLE,
    AU_STUDENT_TELESCOPE_KEYWORDS,
    AU_STUDENT_KEYWORD_ALIASES,
    ARXIV_CATEGORIES,
    ARXIV_GROUPS,
    ARXIV_GROUP_HINTS,
    CATEGORY_HINTS,
)
try:
    from pure_scraper import (
        fetch_orcid_person,
        fetch_orcid_works,
        find_au_colleagues,
        scrape_pure_profile,
        search_pure_profiles,
    )
    _PURE_AVAILABLE = True
except Exception:
    _PURE_AVAILABLE = False

# ─────────────────────────────────────────────────────────────
#  Page config
# ─────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="arXiv Digest Setup",
    page_icon="🔭",
    layout="centered",
)

# ── Custom CSS for brand styling ──
st.markdown(
    f"""
<style>
    @import url('https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=IBM+Plex+Sans:wght@300;400;600&family=DM+Mono:wght@400&display=swap');

    h1, h2, h3 {{ font-family: 'DM Serif Display', Georgia, serif !important; }}
    .stMarkdown p, .stMarkdown li {{ font-family: 'IBM Plex Sans', sans-serif; }}
    code, .stCode {{ font-family: 'DM Mono', monospace !important; }}

    /* Brand card styling */
    .brand-card {{
        background: white;
        border: 1px solid {CARD_BORDER};
        border-radius: 6px;
        padding: 24px;
        margin: 12px 0;
    }}
    .brand-label {{
        font-family: 'DM Mono', monospace;
        font-size: 10px;
        letter-spacing: 0.1em;
        text-transform: uppercase;
        color: {WARM_GREY};
    }}
    .step-number {{
        display: inline-block;
        background: {PINE};
        color: white;
        width: 28px;
        height: 28px;
        border-radius: 50%;
        text-align: center;
        line-height: 28px;
        font-family: 'DM Mono', monospace;
        font-size: 14px;
        margin-right: 8px;
    }}
</style>
""",
    unsafe_allow_html=True,
)


_ORCID_ID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")


def _weight_label(w: int) -> str:
    """Return a human-readable label for a keyword weight (1–10)."""
    if w <= 2:
        return "loosely follow"
    if w <= 5:
        return "interested"
    if w <= 8:
        return "main field"
    return "everything"



def _merge_mini_keywords(track_ids: list[str]) -> dict[str, int]:
    """Merge preset keyword weights, keeping the highest weight per term."""
    merged: dict[str, int] = {}
    for track_id in track_ids:
        for keyword, weight in ASTRO_MINI_TRACKS.get(track_id, {}).get(
            "keywords", {}
        ).items():
            merged[keyword] = max(merged.get(keyword, 0), weight)
    return dict(sorted(merged.items(), key=lambda item: (-item[1], item[0].lower())))


def _merge_keyword_weights(*keyword_maps: dict[str, int]) -> dict[str, int]:
    """Merge weighted keywords, keeping the highest weight per term."""
    merged: dict[str, int] = {}
    for keyword_map in keyword_maps:
        for keyword, weight in (keyword_map or {}).items():
            clean = str(keyword).strip()
            if not clean:
                continue
            merged[clean] = max(merged.get(clean, 0), int(weight))
    return dict(sorted(merged.items(), key=lambda item: (-item[1], item[0].lower())))


def _dedupe_titles(*title_lists: list[str]) -> list[str]:
    """Deduplicate titles while preserving the first-seen order."""
    seen: set[str] = set()
    merged: list[str] = []
    for title_list in title_lists:
        for title in title_list or []:
            clean = title.strip()
            if clean and clean not in seen:
                seen.add(clean)
                merged.append(clean)
    return merged


def _merge_works_meta(*meta_lists: list[dict]) -> list[dict]:
    """Merge ORCID work metadata and keep the most recent year per title."""
    ordered_titles: list[str] = []
    by_title: dict[str, dict] = {}
    for meta_list in meta_lists:
        for entry in meta_list or []:
            title = str(entry.get("title", "")).strip()
            if not title:
                continue
            if title not in by_title:
                ordered_titles.append(title)
                by_title[title] = {"title": title, "year": entry.get("year")}
                continue
            existing_year = by_title[title].get("year")
            incoming_year = entry.get("year")
            if existing_year is None or (
                incoming_year is not None and incoming_year > existing_year
            ):
                by_title[title]["year"] = incoming_year
    return [by_title[title] for title in ordered_titles]


def _merge_coauthor_maps(*coauthor_maps: dict[str, str]) -> dict[str, str]:
    """Merge co-author maps, keeping the latest non-empty display name per ORCID."""
    merged: dict[str, str] = {}
    for coauthor_map in coauthor_maps:
        for orcid_id, name in (coauthor_map or {}).items():
            clean_name = str(name).strip()
            if not orcid_id or not clean_name:
                continue
            merged[orcid_id] = clean_name
    return merged


def _merge_coauthor_counts(*count_maps: dict[str, int]) -> dict[str, int]:
    """Merge co-author frequency maps by summing counts per display name."""
    counts: Counter[str] = Counter()
    for count_map in count_maps:
        counts.update({name: int(count) for name, count in (count_map or {}).items()})
    return dict(counts)


def _name_match_patterns(full_name: str) -> list[str]:
    """Return arXiv-friendly match patterns for a person name."""
    clean = " ".join(full_name.split()).strip()
    if not clean:
        return []
    patterns = [clean]
    parts = clean.split()
    if len(parts) >= 2:
        patterns.append(f"{parts[-1]}, {parts[0][0]}")
    return list(dict.fromkeys(patterns))


def _group_member_names(extra_name: str = "") -> set[str]:
    """Return the lower-cased imported group member names."""
    names = {
        str(member.get("name", "")).strip().lower()
        for member in st.session_state.get("group_orcid_members", [])
        if str(member.get("name", "")).strip()
    }
    if extra_name.strip():
        names.add(extra_name.strip().lower())
    return names


def _set_selected_papers(titles: list[str]) -> None:
    """Keep the paper multiselect widget and backing state in sync."""
    selection = _dedupe_titles(titles)
    st.session_state.selected_papers = selection
    st.session_state["paper_selector_widget"] = selection


def _build_mini_research_context(track_ids: list[str]) -> str:
    """Return a simple weekly student-facing research context from selected tracks."""
    labels = [ASTRO_MINI_TRACKS[t]["label"] for t in track_ids if t in ASTRO_MINI_TRACKS]
    if not labels:
        labels = [ASTRO_MINI_TRACKS["general_astronomy"]["label"]]
    if len(labels) == 1:
        focus = labels[0]
        return (
            f"I am a student following {focus.lower()}. "
            "Please prioritise the most important and readable new astronomy papers in this area each week. "
            "Favour major discoveries, strong review-style papers, landmark observations, and papers likely to matter for learning the field."
        )
    focus = ", ".join(labels[:-1]) + f", and {labels[-1]}"
    return (
        f"I am a student following astronomy topics including {focus.lower()}. "
        "Please prioritise the most important and readable new papers each week. "
        "Favour major discoveries, strong review-style papers, landmark observations, and papers likely to matter for learning the field."
    )


def _build_mini_student_config(
    track_ids: list[str], smtp_server: str, smtp_port: int, github_repo: str
) -> tuple[dict, str]:
    """Build a minimal weekly astronomy-student config and matching cron expression."""
    selected = track_ids or ["general_astronomy"]
    categories: list[str] = []
    for track_id in selected:
        for category in ASTRO_MINI_TRACKS.get(track_id, {}).get("categories", []):
            if category not in categories:
                categories.append(category)

    keywords = _merge_mini_keywords(selected)
    labels = [ASTRO_MINI_TRACKS[t]["label"] for t in selected if t in ASTRO_MINI_TRACKS]
    display_name = labels[0] if len(labels) == 1 else "Astronomy"
    digest_name = f"{display_name} Weekly"

    config = {
        "digest_name": digest_name,
        "researcher_name": display_name,
        "research_context": _build_mini_research_context(selected),
        "categories": categories,
        "keywords": keywords,
        "self_match": [],
        "research_authors": [],
        "colleagues": {"people": [], "institutions": []},
        "digest_mode": "highlights",
        "recipient_view_mode": "5_min_skim",
        "days_back": 8,
        "schedule": "weekly",
        "send_hour_utc": 7,
        "institution": "",
        "department": "",
        "tagline": "",
        "smtp_server": smtp_server,
        "smtp_port": smtp_port,
        "github_repo": github_repo or "",
        "max_papers": 5,
        "min_score": 5,
    }
    return config, "0 7 * * 1"


def _build_au_student_research_context(
    track_ids: list[str], reading_mode: str
) -> str:
    """Return the AU-student weekly research context used for AI scoring."""
    labels = [
        AU_STUDENT_TRACK_LABELS[track_id]
        for track_id in track_ids
        if track_id in AU_STUDENT_TRACK_LABELS and track_id != "au_astronomy"
    ]
    if not labels:
        labels = [
            label
            for track_id, label in AU_STUDENT_TRACK_LABELS.items()
            if track_id != "au_astronomy"
        ]
    focus = ", ".join(labels[:-1]) + f", and {labels[-1]}" if len(labels) > 1 else labels[0]
    if reading_mode == "biggest_only":
        return (
            f"I am an Aarhus University astronomy student following {focus.lower()}. "
            "Prioritise only the most important new papers each week: landmark observations, major theory advances, major surveys or data releases, and strong review-style papers that help build intuition quickly. "
            "Also surface papers connected to Aarhus astronomy, AU-run telescopes, or AU student space projects."
        )
    return (
        f"I am an Aarhus University astronomy student following {focus.lower()}. "
        "Prioritise readable and important papers each week: clear observational results, strong review-style papers, major surveys or data releases, and discoveries that are useful for learning the field. "
        "Also surface papers connected to Aarhus astronomy, AU-run telescopes, or AU student space projects."
    )


def _build_au_student_config(
    student_name: str, student_email: str, track_ids: list[str], reading_mode: str
) -> dict:
    """Build a hidden AU-student digest config with AU astronomy defaults."""
    selected = track_ids or list(AU_STUDENT_TRACK_LABELS.keys())
    categories: list[str] = []
    for track_id in selected:
        for category in ASTRO_MINI_TRACKS.get(track_id, {}).get("categories", []):
            if category not in categories:
                categories.append(category)

    keywords = _merge_keyword_weights(
        _merge_mini_keywords(selected),
        AU_STUDENT_TELESCOPE_KEYWORDS,
    )

    config = {
        "digest_name": "AU Astronomy Student Weekly",
        "researcher_name": student_name.strip() or "AU Astronomy Student",
        "recipient_email": student_email.strip(),
        "student_tracks": list(
            dict.fromkeys(
                [AU_STUDENT_ALWAYS_TAG]
                + [
                    AU_STUDENT_TRACK_LABELS[track_id]
                    for track_id in selected
                    if track_id in AU_STUDENT_TRACK_LABELS
                ]
            )
        ),
        "research_context": _build_au_student_research_context(
            selected, reading_mode
        ),
        "categories": categories,
        "keywords": keywords,
        "keyword_aliases": dict(AU_STUDENT_KEYWORD_ALIASES),
        "self_match": [],
        "research_authors": [person["name"] for person in AU_ASTRONOMY_PEOPLE],
        "colleagues": {"people": list(AU_ASTRONOMY_PEOPLE), "institutions": []},
        "digest_mode": "highlights",
        "recipient_view_mode": "5_min_skim",
        "days_back": 8,
        "schedule": "weekly",
        "send_hour_utc": 7,
        "institution": "Aarhus University",
        "department": "Department of Physics and Astronomy",
        "tagline": "Weekly astronomy reading for AU students",
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 587,
        "github_repo": "",
        "max_papers": 4 if reading_mode == "biggest_only" else 6,
        "min_score": 6 if reading_mode == "biggest_only" else 4,
    }
    return config


def render_mini_setup() -> None:
    """Render a separate mini setup flow for students without ORCID."""
    st.markdown("## Mini setup — no ORCID")
    st.markdown(
        "For students who do not have an ORCID yet. Pick a few astronomy interests and get a simple weekly digest of the most important papers in those areas."
    )

    selected_tracks = st.multiselect(
        "Astronomy interests",
        options=list(ASTRO_MINI_TRACKS.keys()),
        default=["general_astronomy"],
        format_func=lambda key: ASTRO_MINI_TRACKS[key]["label"],
        help="Choose one or more areas you want to follow.",
    )
    if not selected_tracks:
        st.info("Pick at least one interest to generate a mini config.")
        return

    if "general_astronomy" in selected_tracks and len(selected_tracks) > 1:
        st.caption(
            "General astronomy overlaps the more specific tracks below, so the digest will stay broad."
        )

    st.markdown("**What this mini setup will do**")
    st.caption(
        "Weekly on Monday, 5-minute skim format, up to 5 papers, and no profile import or name matching."
    )

    with st.expander("Selected tracks", expanded=True):
        for track_id in selected_tracks:
            track = ASTRO_MINI_TRACKS[track_id]
            st.markdown(f"**{track['label']}**")
            st.caption(track["blurb"])

    smtp_options = {
        "Gmail": ("smtp.gmail.com", 587),
        "Outlook / Office 365": ("smtp.office365.com", 587),
    }
    smtp_choice = st.radio(
        "SMTP provider",
        options=list(smtp_options.keys()),
        horizontal=True,
        label_visibility="collapsed",
    )
    smtp_server, smtp_port = smtp_options[smtp_choice]

    github_repo = st.text_input(
        "GitHub repo (optional)",
        placeholder="username/arxiv-digest",
        help="Enables self-service links in emails",
        key="mini_github_repo",
    )

    config, cron_expr = _build_mini_student_config(
        selected_tracks, smtp_server, smtp_port, github_repo
    )
    config_yaml = yaml.dump(
        config, default_flow_style=False, sort_keys=False, allow_unicode=True
    )

    st.markdown("### Your config.yaml is ready")
    tab1, tab2 = st.tabs(["config.yaml", "Workflow cron"])

    with tab1:
        st.code(config_yaml, language="yaml")

    with tab2:
        st.markdown(
            "Use this cron line in `.github/workflows/digest.yml` for the student mini setup:"
        )
        st.code(
            "    - cron: '0 7 * * 1'  # Once a week (Monday) at 07:00 UTC",
            language="yaml",
        )

    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            label="📥 Download config.yaml",
            data=config_yaml,
            file_name="config.yaml",
            mime="text/yaml",
            type="primary",
            use_container_width=True,
        )
    with col2:
        if st.button("📋 Show YAML", use_container_width=True, key="mini_copy"):
            st.code(config_yaml, language="yaml")
            st.info("Select all text above and copy with Ctrl+C (Cmd+C on Mac)")

    st.divider()
    st.markdown("## Next Steps")
    st.markdown(
        f"""
<div class="brand-card">
<p><span class="step-number">1</span> <strong>Fork the template repo</strong></p>
<p style="margin-left: 36px;">
Fork <a href="https://github.com/SilkeDainese/arxiv-digest" target="_blank">SilkeDainese/arxiv-digest</a>.
</p>

<p><span class="step-number">2</span> <strong>Upload your config.yaml</strong></p>
<p style="margin-left: 36px;">
In your fork, click <strong>Add file → Upload files</strong> and upload the <code>config.yaml</code>
you just downloaded.
</p>

<p><span class="step-number">3</span> <strong>Add secrets</strong></p>
<p style="margin-left: 36px;">
Add <code>RECIPIENT_EMAIL</code> plus either <code>DIGEST_RELAY_TOKEN</code> or your own
<code>SMTP_USER</code> and <code>SMTP_PASSWORD</code> in your fork’s
<strong>Settings → Secrets and variables → Actions</strong>.
</p>

<p><span class="step-number">4</span> <strong>Switch the workflow to weekly</strong></p>
<p style="margin-left: 36px;">
Replace the cron line in <code>.github/workflows/digest.yml</code> with:
</p>
<pre style="margin-left: 36px;"><code>    - cron: '{cron_expr}'</code></pre>
</div>
""",
        unsafe_allow_html=True,
    )


def render_au_student_setup() -> None:
    """Render the hidden AU-student setup flow."""
    st.markdown("## AU astronomy student setup")
    st.markdown(
        "Hidden lightweight setup for Aarhus astronomy students. Enter the student's name and email, then pick the astronomy areas they should follow."
    )

    student_name = st.text_input(
        "Student name",
        placeholder="Astronomy student",
        key="au_student_name",
    )
    student_email = st.text_input(
        "Student email",
        placeholder="student@post.au.dk",
        key="au_student_email",
    )

    selected_tracks = st.multiselect(
        "Astronomy interests",
        options=list(AU_STUDENT_TRACK_LABELS.keys()),
        default=list(AU_STUDENT_TRACK_LABELS.keys()),
        format_func=lambda key: AU_STUDENT_TRACK_LABELS[key],
        help="These tracks are biased toward readable and important papers for students.",
    )

    reading_mode = st.radio(
        "Weekly style",
        options=["simple_and_important", "biggest_only"],
        format_func=lambda mode: {
            "simple_and_important": "Simple + important",
            "biggest_only": "Only the biggest papers",
        }[mode],
        horizontal=True,
    )

    st.markdown("**Always included on top of the selected tracks**")
    st.caption(
        "AU Astronomy is included for every student digest: AU astronomy papers, AU-run telescope keywords, and AU student-space projects."
    )
    st.caption(
        f"`{AU_STUDENT_ALWAYS_TAG}` is preselected here as a visible tag, and it stays included for every student even if someone unticks it."
    )

    if not student_email.strip():
        st.info("Enter an email to generate the AU-student config.")
        return
    if not selected_tracks:
        st.info("Pick at least one astronomy area.")
        return

    config = _build_au_student_config(student_name, student_email, selected_tracks, reading_mode)
    config_yaml = yaml.dump(
        config, default_flow_style=False, sort_keys=False, allow_unicode=True
    )

    st.markdown("### AU student config.yaml")
    st.code(config_yaml, language="yaml")
    st.caption(
        "This mode is designed for a shared AU-student digest preset. It includes the recipient email directly in config.yaml."
    )

    st.download_button(
        label="📥 Download AU student config.yaml",
        data=config_yaml,
        file_name="config.yaml",
        mime="text/yaml",
        type="primary",
        use_container_width=True,
    )



def suggest_categories(text: str) -> list[str]:
    """Return up to 6 relevant arXiv category codes for the given research description.

    Uses AI when available for field-agnostic suggestions; falls back to keyword
    matching against CATEGORY_HINTS.
    """
    if _ai_available():
        cat_list = "\n".join(
            f"  {code}: {name}" for code, name in ARXIV_CATEGORIES.items()
        )
        prompt = (
            f'A researcher describes their work as:\n"{text}"\n\n'
            f"Here is the full list of arXiv categories:\n{cat_list}\n\n"
            "Return ONLY a JSON array of the 4–6 most relevant category codes "
            '(e.g. ["cond-mat.supr-con", "cond-mat.mes-hall"]). '
            "Pick the best-matching sub-categories — never return a bare top-level code "
            "like 'cond-mat' or 'astro-ph' unless it appears exactly in the list above. "
            "No explanation, no other text."
        )
        raw = _call_ai(prompt)
        if raw:
            try:
                raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
                raw = re.sub(r"\n?```$", "", raw)
                cats = json.loads(raw)
                # Keep only codes that actually exist in our catalogue
                valid = [c for c in cats if c in ARXIV_CATEGORIES]
                if valid:
                    return valid[:6]
            except Exception:
                pass  # fall through to regex fallback

    # Regex fallback — keyword overlap
    text_lower = text.lower()
    scores = {}
    for cat, hints in CATEGORY_HINTS.items():
        if cat not in ARXIV_CATEGORIES:
            continue
        score = sum(1 for h in hints if h.lower() in text_lower)
        if score >= 2:
            scores[cat] = score
    return sorted(scores, key=scores.get, reverse=True)[:6]


def _call_ai(prompt: str, max_tokens: int = 512) -> str | None:
    """Call Gemini (preferred, free tier) or Claude. Returns text or None on failure."""
    # Try Gemini first — free tier available via Google AI Studio
    gemini_key = _get_gemini_key()
    if gemini_key and _GEMINI_AVAILABLE:
        try:
            _client = _genai_lib.Client(api_key=gemini_key)
            response = _client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
            )
            return response.text.strip()
        except Exception:
            pass

    # Fall back to Anthropic
    anthropic_key = _get_anthropic_key()
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


@st.cache_data(show_spinner=False)
def _test_ai_key(gemini_key: str, anthropic_key: str) -> tuple[bool, str, str]:
    """
    Validate that at least one AI key actually works.

    Cached by key values so it only runs once per unique key combination.

    Returns (ok, provider_name, error_message).
    """
    if gemini_key and _GEMINI_AVAILABLE:
        try:
            _client = _genai_lib.Client(api_key=gemini_key)
            _client.models.generate_content(
                model="gemini-2.0-flash",
                contents="Hi",
                config=_genai_types.GenerateContentConfig(max_output_tokens=1),
            )
            return True, "Gemini", ""
        except Exception as e:
            gemini_err = str(e)
    else:
        gemini_err = ""

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
    else:
        anthropic_err = ""

    errors = []
    if gemini_err:
        errors.append(f"Gemini: {gemini_err}")
    if anthropic_err:
        errors.append(f"Anthropic: {anthropic_err}")
    return False, "", "  |  ".join(errors) if errors else "No valid key entered."


def draft_research_description(keywords: dict[str, int]) -> str:
    """Generate a first-person research description from keywords using AI."""
    top_keywords = [k for k, _ in sorted(keywords.items(), key=lambda x: -x[1])[:10]]
    prompt = (
        f"A researcher has these keywords extracted from their publications:\n"
        f"{', '.join(top_keywords)}\n\n"
        "Write a 3-4 sentence research description in first person (starting with 'I') "
        "that this researcher could use to describe their work to a colleague. "
        "Be specific and technical. Return only the description, no other text."
    )
    result = _call_ai(prompt, max_tokens=200)
    return (
        result if result else f"My research focuses on {', '.join(top_keywords[:5])}."
    )


def _summarise_research(titles: list[str]) -> str:
    """Generate a first-person research summary from publication titles using AI."""
    sample = titles[:30]  # Cap to avoid token limits
    titles_block = "\n".join(f"- {t}" for t in sample)
    prompt = (
        "Here are publication titles from a researcher's ORCID profile:\n"
        f"{titles_block}\n\n"
        "Write a 3-4 sentence research description in first person (starting with 'I') "
        "that captures what this researcher works on. Be specific about methods, objects, "
        "or phenomena — avoid generic filler. Return only the description, no other text."
    )
    result = _call_ai(prompt, max_tokens=200)
    return result or ""


def _get_gemini_key() -> str | None:
    """Return Gemini API key: user-provided → invite-unlocked shared key → None."""
    user_key = st.session_state.get("user_gemini_key", "").strip()
    if user_key:
        return user_key

    invite_bundle = st.session_state.get("_invite_bundle", {})
    invite_key = str(invite_bundle.get("gemini_api_key", "")).strip()
    if invite_key:
        return invite_key
    return None


def _get_anthropic_key() -> str | None:
    """Return Anthropic API key: user-provided → invite-unlocked shared key → None."""
    user_key = st.session_state.get("user_anthropic_key", "").strip()
    if user_key:
        return user_key

    invite_bundle = st.session_state.get("_invite_bundle", {})
    invite_key = str(invite_bundle.get("anthropic_api_key", "")).strip()
    if invite_key:
        return invite_key
    return None


def _ai_available() -> bool:
    """True if any AI key is configured."""
    return bool(
        (_get_gemini_key() and _GEMINI_AVAILABLE)
        or (_get_anthropic_key() and _ANTHROPIC_AVAILABLE)
    )


def _keyword_regex_fallback(text: str) -> dict[str, int]:
    """Extract keywords from research description using pattern matching (no API needed)."""
    words = text.split()
    candidates: dict[str, int] = {}
    stopwords = {
        "i",
        "my",
        "me",
        "we",
        "our",
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "as",
        "is",
        "was",
        "are",
        "were",
        "been",
        "be",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "that",
        "which",
        "who",
        "this",
        "these",
        "it",
        "its",
        "their",
        "also",
        "using",
        "such",
        "both",
        "between",
        "about",
        "into",
        "through",
        "particularly",
        "specifically",
        "especially",
        "including",
        "focus",
        "work",
        "study",
        "research",
        "currently",
        "mainly",
        "primarily",
    }
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
        if w1 not in stopwords and w2 not in stopwords and len(w1) > 2 and len(w2) > 2:
            bigram = f"{w1} {w2}"
            if bigram not in candidates:
                candidates[bigram] = 7

    for i in range(len(clean_words) - 2):
        w1, w2, w3 = (
            clean_words[i].lower(),
            clean_words[i + 1].lower(),
            clean_words[i + 2].lower(),
        )
        if all(w not in stopwords and len(w) > 2 for w in (w1, w2, w3)):
            trigram = f"{w1} {w2} {w3}"
            if len(trigram) > 10:
                candidates[trigram] = 9

    generic = {"et al", "ground based", "non linear"}
    return {
        k: v
        for k, v in sorted(candidates.items(), key=lambda x: -x[1])[:25]
        if k.lower() not in generic
    }


def suggest_keywords_from_context(
    text: str, orcid_keywords: dict | None = None
) -> dict[str, int]:
    """Score research keywords by relevance using Claude if available, regex otherwise.

    When orcid_keywords is provided, Claude re-scores those publication-derived keywords
    against the research description so that frequency in titles doesn't dominate.
    """
    api_key = _get_anthropic_key()

    if not _ai_available():
        return _keyword_regex_fallback(text)

    # Build candidate list: ORCID keywords + regex-derived keywords from description
    regex_kws = _keyword_regex_fallback(text)
    all_candidates = dict(regex_kws)
    if orcid_keywords:
        all_candidates.update(orcid_keywords)

    candidate_list = "\n".join(f"- {kw}" for kw in all_candidates)

    prompt = (
        f'A researcher describes their work as:\n"{text}"\n\n'
        f"These are candidate keywords (some from publication titles, some from the description):\n"
        f"{candidate_list}\n\n"
        "Score each keyword's relevance to this researcher's specific field on a scale of 1–10. "
        "Prefer specific technical terms over generic words. Generic words that happen to appear "
        "in paper titles (like 'water', 'worlds', 'population') should score low unless they are "
        "genuinely central to this specific research. Return at most 25 keywords. "
        "Return ONLY a JSON object mapping each keyword to its integer score. No other text."
    )

    raw = _call_ai(prompt)
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


# ─────────────────────────────────────────────────────────────
#  Profile import helpers
# ─────────────────────────────────────────────────────────────


def _apply_orcid_keywords(keywords: dict, orcid_url: str = "") -> None:
    """Merge ORCID keywords into session state and auto-draft description."""
    if keywords:
        st.session_state.keywords = _merge_keyword_weights(
            st.session_state.keywords, keywords
        )
        if not st.session_state.research_description:
            if _ai_available():
                _drafted = draft_research_description(st.session_state.keywords)
            else:
                _top_kws = [
                    k
                    for k, _ in sorted(
                        st.session_state.keywords.items(), key=lambda x: -x[1]
                    )[:5]
                ]
                _drafted = (
                    f"My research focuses on {', '.join(_top_kws)}." if _top_kws else ""
                )
            if _drafted:
                st.session_state.research_description = _drafted
                st.session_state._research_description_val = _drafted
                st.session_state["research_description_widget"] = _drafted
    st.session_state.pure_scanned = True
    if orcid_url:
        st.session_state.pure_confirmed_url = orcid_url


def _apply_pure_keywords(keywords: dict | None, coauthors: list | None) -> None:
    """Merge Pure-scraped keywords and co-authors into session state."""
    if keywords:
        merged = dict(st.session_state.keywords)
        merged.update(keywords)
        st.session_state.keywords = merged
    if coauthors:
        for name in coauthors[:15]:
            parts = name.split()
            if len(parts) >= 2:
                match_pattern = f"{parts[-1]}, {parts[0][0]}"
                if not any(
                    c["name"] == name for c in st.session_state.colleagues_people
                ):
                    st.session_state.colleagues_people.append(
                        {"name": name, "match": [match_pattern]}
                    )
    st.session_state.pure_scanned = True


def _maybe_seed_research_description(
    research_summary: str = "",
    keywords: dict | None = None,
    titles: list[str] | None = None,
) -> None:
    """Seed the editable research description if the user has not written one yet."""
    if st.session_state.research_description:
        return

    drafted = ""
    if research_summary:
        drafted = research_summary
    elif keywords:
        if _ai_available():
            drafted = draft_research_description(keywords)
        else:
            top_keywords = [k for k, _ in sorted(keywords.items(), key=lambda x: -x[1])[:5]]
            drafted = (
                f"My research focuses on {', '.join(top_keywords)}."
                if top_keywords
                else ""
            )
    elif titles:
        top_titles = list(titles)[:5]
        words: list[str] = []
        stopwords = {
            "a",
            "an",
            "the",
            "of",
            "in",
            "on",
            "at",
            "to",
            "for",
            "and",
            "or",
            "with",
            "from",
            "by",
            "is",
            "are",
            "using",
        }
        for title in top_titles:
            for word in title.split():
                clean = re.sub(r"[^a-zA-Z\\-]", "", word)
                if len(clean) > 4 and clean.lower() not in stopwords:
                    words.append(clean)
        unique_words = list(dict.fromkeys(words))[:6]
        if unique_words:
            drafted = (
                f"My research focuses on {', '.join(unique_words[:-1])}, and {unique_words[-1]}. "
                f"These topics span my recent publications in areas including "
                f"{', '.join(w.lower() for w in unique_words[:3])}."
            )

    if drafted:
        st.session_state.research_description = drafted
        st.session_state._research_description_val = drafted
        st.session_state["research_description_widget"] = drafted


def _import_profile(result: dict) -> None:
    """Full import from a single ORCID search result: fill profile + extract keywords."""
    # Pre-fill profile fields
    st.session_state.profile_name = result["name"]
    if result.get("department"):
        st.session_state.profile_institution = result["department"]
    st.session_state.pure_confirmed_url = result["url"]

    # Extract keywords from publications
    orcid_id = result["url"].rstrip("/").split("/")[-1]
    if not _PURE_AVAILABLE:
        st.info("Pure lookup is temporarily unavailable — using ORCID only.")
        return
    (
        keywords,
        _titles,
        _works_meta,
        _coauthor_map,
        _coauthor_counts,
        error,
    ) = fetch_orcid_works(orcid_id)
    # Persist titles, works meta, and coauthor map for paper selector and suggested colleagues
    st.session_state["_orcid_titles"] = _titles or []
    st.session_state["_orcid_works_meta"] = _works_meta or []
    st.session_state["_orcid_coauthor_map"] = (
        dict(_coauthor_map) if _coauthor_map else {}
    )
    st.session_state["_orcid_coauthor_counts"] = (
        dict(_coauthor_counts) if _coauthor_counts else {}
    )
    if not error and keywords:
        _apply_orcid_keywords(keywords, orcid_url=result["url"])
    else:
        st.session_state.pure_scanned = True
        st.session_state.pure_confirmed_url = result["url"]

    st.rerun()


# ─────────────────────────────────────────────────────────────
#  Server-key mode + rate limiting
# ─────────────────────────────────────────────────────────────


def _has_server_key() -> bool:
    """True if the current invite code unlocked shared AI credentials."""
    invite_bundle = st.session_state.get("_invite_bundle", {})
    return bool(
        str(invite_bundle.get("gemini_api_key", "")).strip()
        or str(invite_bundle.get("anthropic_api_key", "")).strip()
    )


def _normalise_invite_bundles(raw: object) -> dict[str, dict[str, str]]:
    """Return invite-code bundles in a consistent {code: secrets} shape."""
    if not isinstance(raw, Mapping):
        return {}

    bundles: dict[str, dict[str, str]] = {}
    for code, payload in raw.items():
        clean_code = str(code).strip()
        if not clean_code:
            continue

        if isinstance(payload, str):
            token = payload.strip()
            if token:
                bundles[clean_code] = {"relay_token": token}
            continue

        if not isinstance(payload, Mapping):
            continue

        relay_token = str(
            payload.get("relay_token") or payload.get("digest_relay_token") or ""
        ).strip()
        gemini_key = str(payload.get("gemini_api_key") or "").strip()
        anthropic_key = str(payload.get("anthropic_api_key") or "").strip()

        secrets_bundle = {
            key: value
            for key, value in {
                "relay_token": relay_token,
                "gemini_api_key": gemini_key,
                "anthropic_api_key": anthropic_key,
            }.items()
            if value
        }
        if secrets_bundle:
            bundles[clean_code] = secrets_bundle

    return bundles


def _get_invite_bundle(code: str) -> dict[str, str]:
    """Look up an invite code from Streamlit secrets and return its secret bundle."""
    invite_code = code.strip()
    if not invite_code:
        return {}

    raw_sources: list[object] = []
    try:
        json_blob = st.secrets.get("INVITE_CODES_JSON")
        if json_blob:
            raw_sources.append(json.loads(json_blob))
    except Exception:
        pass

    for key in ("invite_codes", "INVITE_CODES"):
        try:
            raw_sources.append(st.secrets.get(key))
        except Exception:
            pass

    bundles: dict[str, dict[str, str]] = {}
    for raw in raw_sources:
        bundles.update(_normalise_invite_bundles(raw))

    for candidate_code, bundle in bundles.items():
        if hmac.compare_digest(invite_code, candidate_code.strip()):
            return bundle
    return {}


# Module-level dict: email (lowercased) → unix timestamp of last use.
# Persists for the lifetime of the Streamlit process (resets on redeploy).
_DAILY_USAGE: dict[str, float] = {}
_RATE_LIMIT_SECONDS = 86400  # 24 hours


def _is_rate_limited(email: str) -> bool:
    last = _DAILY_USAGE.get(email.lower().strip(), 0.0)
    return (time.time() - last) < _RATE_LIMIT_SECONDS


def _record_usage(email: str) -> None:
    _DAILY_USAGE[email.lower().strip()] = time.time()


# ─────────────────────────────────────────────────────────────
#  Session state defaults
# ─────────────────────────────────────────────────────────────

if "keywords" not in st.session_state:
    st.session_state.keywords = {}
if "colleagues_people" not in st.session_state:
    st.session_state.colleagues_people = []
if "colleagues_institutions" not in st.session_state:
    st.session_state.colleagues_institutions = []
if "research_authors" not in st.session_state:
    st.session_state.research_authors = []
if "pure_scanned" not in st.session_state:
    st.session_state.pure_scanned = False
if "self_match" not in st.session_state:
    st.session_state.self_match = []
if "ai_suggested_cats" not in st.session_state:
    st.session_state.ai_suggested_cats = []
if "ai_suggested_kws" not in st.session_state:
    st.session_state.ai_suggested_kws = {}
# Profile prefill from ORCID scan
if "profile_name" not in st.session_state:
    st.session_state.profile_name = ""
if "profile_institution" not in st.session_state:
    st.session_state.profile_institution = ""
if "profile_department" not in st.session_state:
    st.session_state.profile_department = ""
# Research description (editable, can be auto-drafted from publications).
# _research_description_val is the backing store; the text_area widget uses a
# separate key so that programmatic updates (e.g. from ORCID import) are
# reflected on the next render without the widget state shadowing the value.
if "research_description" not in st.session_state:
    st.session_state.research_description = ""
if "_research_description_val" not in st.session_state:
    st.session_state._research_description_val = ""
# Wizard step tracking — 1-indexed, controls which expander is open
if "current_step" not in st.session_state:
    st.session_state.current_step = 1
if "profile_mode" not in st.session_state:
    st.session_state.profile_mode = "individual"
# Paper selector: subset of fetched ORCID titles to use for keyword/category AI suggestions
if "selected_papers" not in st.session_state:
    st.session_state.selected_papers = []
if "_orcid_coauthor_counts" not in st.session_state:
    st.session_state["_orcid_coauthor_counts"] = {}
if "group_orcid_members" not in st.session_state:
    st.session_state.group_orcid_members = []
if "_invite_bundle" not in st.session_state:
    st.session_state["_invite_bundle"] = {}


# ─────────────────────────────────────────────────────────────
#  Welcome
# ─────────────────────────────────────────────────────────────

st.markdown("# 🔭 arXiv Digest Setup")
st.markdown("""
Set up your personal arXiv digest in 5 minutes. This wizard generates a `config.yaml`
that you drop into your GitHub fork — then you'll get curated papers delivered to your inbox.
""")

st.markdown(
    f"""
<div style="font-family: 'DM Mono', monospace; font-size: 10px; letter-spacing: 0.1em;
     text-transform: uppercase; color: {WARM_GREY}; margin-top: -8px; margin-bottom: 24px;">
     Built by <a href="https://silkedainese.github.io" style="color: {PINE};">Silke S. Dainese</a>
</div>
""",
    unsafe_allow_html=True,
)

hidden_setup_mode = str(st.query_params.get("setup", "")).strip().lower()
if hidden_setup_mode == "au_students":
    render_au_student_setup()
    st.stop()

setup_mode = st.radio(
    "Setup mode",
    options=["full_researcher", "mini_no_orcid"],
    format_func=lambda mode: {
        "full_researcher": "Researcher setup — ORCID, profile import, fine control",
        "mini_no_orcid": "Mini setup — no ORCID, just pick astronomy interests",
    }[mode],
    horizontal=True,
    label_visibility="collapsed",
)

if setup_mode == "mini_no_orcid":
    render_mini_setup()
    st.stop()

# ── Invite code (optional) ──
st.markdown("### Invite code")
invite_code = st.text_input(
    "Invite code (optional)",
    key="invite_code_input",
    type="password",
    placeholder="Only if Silke gave you one",
    help="Unlocks shared relay / AI access for invited users.",
)
invite_bundle = _get_invite_bundle(invite_code)
st.session_state["_invite_bundle"] = invite_bundle
if invite_code.strip():
    if invite_bundle:
        unlocked = []
        if invite_bundle.get("relay_token"):
            unlocked.append("relay")
        if invite_bundle.get("gemini_api_key") or _has_server_key():
            unlocked.append("Gemini")
        if invite_bundle.get("anthropic_api_key"):
            unlocked.append("Anthropic")
        st.success(
            "Invite code accepted — shared "
            + ", ".join(unlocked)
            + " access is enabled for this session."
        )
    else:
        st.warning("Invite code not recognised.")

# ── AI setup — invite-unlocked shared key or bring-your-own ──
if _has_server_key():
    # Server has a key — just ask for email for rate limiting
    st.markdown("## Get started")
    st.markdown("AI is included — no API key needed. Enter your email to begin.")
    user_email = st.text_input(
        "Your email",
        placeholder="you@university.edu",
        key="user_email_rl",
    )
    if not user_email.strip():
        st.stop()
    if _is_rate_limited(user_email):
        st.warning(
            "You've already generated a config today. "
            "Come back tomorrow, or [get your own free Gemini key](https://aistudio.google.com/app/apikey) to run without limits."
        )
        st.stop()
    _record_usage(user_email)
    st.success("Ready — AI powered by Gemini.")
else:
    # No server key — user must bring their own
    st.markdown("## Choose your AI")
    st.markdown(
        "AI is used throughout — for finding your profile, suggesting keywords, and scoring papers in your daily digest. "
        "Your key is only used during this session and never stored."
    )
    col_g, col_a = st.columns(2)
    with col_g:
        st.markdown("**Gemini** — free tier, no credit card")
        st.text_input(
            "Gemini API key",
            type="password",
            placeholder="AIza...",
            key="user_gemini_key",
            label_visibility="collapsed",
            help="Get a free key at aistudio.google.com",
        )
        st.caption("[Get a free key →](https://aistudio.google.com/app/apikey)")
    with col_a:
        st.markdown("**Anthropic** — Claude")
        st.text_input(
            "Anthropic API key",
            type="password",
            placeholder="sk-ant-...",
            key="user_anthropic_key",
            label_visibility="collapsed",
            help="Get a key at console.anthropic.com",
        )
        st.caption("[Get a key →](https://console.anthropic.com/settings/keys)")

    if _ai_available():
        with st.spinner("Checking key..."):
            _key_ok, _provider, _key_err = _test_ai_key(
                _get_gemini_key() or "", _get_anthropic_key() or ""
            )
        if _key_ok:
            st.success(f"AI ready — using {_provider}.")
        else:
            st.error(f"Key didn't work: {_key_err}")
            st.stop()
    else:
        st.warning(
            "Enter an API key above to continue. AI is required for profile search and paper scoring."
        )
        st.stop()

ai_assist = True  # AI is always on when we reach this point

st.divider()

profile_mode = st.radio(
    "Who is this digest for?",
    options=["individual", "group"],
    format_func=lambda mode: {
        "individual": "Individual researcher",
        "group": "Research group / journal club",
    }[mode],
    horizontal=True,
    index=0 if st.session_state.profile_mode == "individual" else 1,
)
st.session_state.profile_mode = profile_mode


# ─────────────────────────────────────────────────────────────
#  Section 1: Profile Scan (optional)
# ─────────────────────────────────────────────────────────────

if "pure_confirmed_url" not in st.session_state:
    st.session_state.pure_confirmed_url = ""
if "orcid_preview" not in st.session_state:
    st.session_state.orcid_preview = (
        None  # dict with name/institution/orcid_url/keywords when pending
    )


def _commit_preview() -> None:
    """Write the staged orcid_preview into session state and mark as scanned."""
    p = st.session_state.orcid_preview
    if not p:
        return

    full_name = p.get("name", "").strip()
    member_name_blocklist = _group_member_names(full_name)

    # Add confirmed AU colleagues, excluding imported group members.
    for name in p.get("selected_colleagues", []):
        if name.strip().lower() in member_name_blocklist:
            continue
        parts = name.split()
        if len(parts) >= 2:
            match_pattern = f"{parts[-1]}, {parts[0][0]}"
        else:
            match_pattern = name
        if not any(c["name"] == name for c in st.session_state.colleagues_people):
            st.session_state.colleagues_people.append(
                {"name": name, "match": [match_pattern]}
            )

    if st.session_state.get("profile_mode") == "group":
        imported_members = list(st.session_state.get("group_orcid_members", []))
        if not any(m.get("orcid_url") == p["orcid_url"] for m in imported_members):
            imported_members.append(
                {
                    "name": full_name,
                    "institution": p.get("institution", ""),
                    "orcid_url": p["orcid_url"],
                    "paper_count": len(p.get("titles", [])),
                }
            )
        st.session_state.group_orcid_members = imported_members

        if p["keywords"]:
            st.session_state.keywords = _merge_keyword_weights(
                st.session_state.keywords, p["keywords"]
            )

        for pattern in _name_match_patterns(full_name):
            if pattern not in st.session_state.self_match:
                st.session_state.self_match.append(pattern)

        st.session_state.colleagues_people = [
            colleague
            for colleague in st.session_state.colleagues_people
            if colleague.get("name", "").strip().lower()
            not in _group_member_names()
        ]

        st.session_state["_orcid_titles"] = _dedupe_titles(
            st.session_state.get("_orcid_titles", []), p.get("titles", [])
        )
        st.session_state["_orcid_works_meta"] = _merge_works_meta(
            st.session_state.get("_orcid_works_meta", []), p.get("works_meta", [])
        )
        st.session_state["_orcid_coauthor_map"] = _merge_coauthor_maps(
            st.session_state.get("_orcid_coauthor_map", {}), p.get("coauthor_map", {})
        )
        st.session_state["_orcid_coauthor_counts"] = _merge_coauthor_counts(
            st.session_state.get("_orcid_coauthor_counts", {}),
            p.get("coauthor_counts", {}),
        )
        st.session_state.selected_papers = [
            title
            for title in st.session_state.selected_papers
            if title in st.session_state["_orcid_titles"]
        ]

        if not st.session_state.profile_institution and p.get("institution"):
            st.session_state.profile_institution = p["institution"]
        if not st.session_state.profile_name:
            st.session_state.profile_name = "Research Group"
        st.session_state.pure_confirmed_url = (
            f"{len(imported_members)} ORCID"
            f"{'' if len(imported_members) == 1 else 's'} imported"
        )
        _maybe_seed_research_description(
            research_summary=p.get("research_summary", ""),
            keywords=st.session_state.keywords,
            titles=st.session_state.get("_orcid_titles", []),
        )
        st.session_state.pure_scanned = bool(imported_members)
    else:
        st.session_state.profile_name = full_name
        st.session_state.profile_institution = p["institution"]
        st.session_state.pure_confirmed_url = p["orcid_url"]

        if p["keywords"]:
            st.session_state.keywords = _merge_keyword_weights(
                st.session_state.keywords, p["keywords"]
            )

        for pattern in _name_match_patterns(full_name):
            if pattern not in st.session_state.self_match:
                st.session_state.self_match.append(pattern)

        st.session_state["_orcid_titles"] = p.get("titles", [])
        st.session_state["_orcid_works_meta"] = p.get("works_meta", [])
        st.session_state["_orcid_coauthor_map"] = p.get("coauthor_map", {})
        st.session_state["_orcid_coauthor_counts"] = p.get("coauthor_counts", {})
        _maybe_seed_research_description(
            research_summary=p.get("research_summary", ""),
            keywords=p.get("keywords", {}),
            titles=p.get("titles", []),
        )
        st.session_state.pure_scanned = True

    st.session_state.orcid_preview = None


with st.expander("**1. Your ORCID**", expanded=(st.session_state.current_step == 1)):
    if profile_mode == "group":
        st.markdown(
            "ORCID import is optional for groups. You can skip this step and configure the group manually, or import up to 8 member ORCIDs to bootstrap shared keywords, categories, and colleague suggestions."
        )
    else:
        st.markdown(
            "Enter your ORCID ID — we'll pull your profile and publications automatically."
        )

    group_members = st.session_state.get("group_orcid_members", [])
    max_group_orcids = 8

    if profile_mode == "group":
        if group_members:
            st.success(
                f"✓ Imported {len(group_members)} member ORCID"
                f"{'' if len(group_members) == 1 else 's'}"
            )
            for member in group_members:
                paper_count = int(member.get("paper_count", 0))
                paper_label = "paper" if paper_count == 1 else "papers"
                st.caption(
                    f"• {member.get('name', 'Unknown member')} · {member.get('institution', 'No institution listed')} · {paper_count} {paper_label}"
                )
            if st.button("↺ Clear imported ORCIDs", type="secondary"):
                st.session_state.pure_scanned = False
                st.session_state.pure_confirmed_url = ""
                st.session_state.orcid_preview = None
                st.session_state.group_orcid_members = []
                st.session_state["_orcid_titles"] = []
                st.session_state["_orcid_works_meta"] = []
                st.session_state["_orcid_coauthor_map"] = {}
                st.session_state["_orcid_coauthor_counts"] = {}
                _set_selected_papers([])
                st.rerun()
        else:
            st.caption(
                "Optional: import a few representative members to seed the group digest automatically."
            )
    elif st.session_state.pure_scanned:
        st.success(f"✓ Profile loaded from {st.session_state.pure_confirmed_url}")
        if st.button("↺ Use a different ORCID", type="secondary"):
            st.session_state.pure_scanned = False
            st.session_state.pure_confirmed_url = ""
            st.session_state.orcid_preview = None
            st.session_state["_orcid_titles"] = []
            st.session_state["_orcid_works_meta"] = []
            st.session_state["_orcid_coauthor_map"] = {}
            st.session_state["_orcid_coauthor_counts"] = {}
            _set_selected_papers([])
            st.rerun()

    can_add_group_member = len(group_members) < max_group_orcids
    show_import_controls = profile_mode == "group" or not st.session_state.pure_scanned

    if show_import_controls:
        # ── ORCID input ──
        col_input, col_btn = st.columns([5, 1])
        with col_input:
            orcid_input = st.text_input(
                "ORCID",
                placeholder="0000-0001-2345-6789  or  https://orcid.org/0000-0001-2345-6789",
                key="orcid_input_field",
                label_visibility="collapsed",
            )
        with col_btn:
            fetch_clicked = st.button(
                "🔍 Fetch" if profile_mode == "individual" else "＋ Add",
                type="primary",
                use_container_width=True,
                disabled=profile_mode == "group" and not can_add_group_member,
            )

        if profile_mode == "group" and not can_add_group_member:
            st.caption(
                f"Imported {max_group_orcids} ORCIDs already. Continue, or clear them and start over."
            )

        if orcid_input and fetch_clicked:
            inp = orcid_input.strip().rstrip("/")
            # Accept bare ID or full URL
            if inp.startswith("https://orcid.org/"):
                orcid_id = inp.split("/")[-1]
                orcid_url = inp
            elif _ORCID_ID_RE.match(inp):
                orcid_id = inp
                orcid_url = f"https://orcid.org/{inp}"
            else:
                st.error(
                    "That doesn't look like an ORCID. Expected format: 0000-0001-2345-6789"
                )
                orcid_id = ""
                orcid_url = ""

            if orcid_id and not _PURE_AVAILABLE:
                st.info("Pure lookup is temporarily unavailable — using ORCID only.")
            elif orcid_id:
                with st.spinner("Fetching profile and publications from ORCID..."):
                    full_name, institution, person_error = fetch_orcid_person(orcid_id)
                    (
                        keywords,
                        titles,
                        works_meta,
                        coauthor_map,
                        coauthor_counts,
                        works_error,
                    ) = (
                        fetch_orcid_works(orcid_id)
                    )

                if person_error:
                    st.error(f"Could not fetch profile: {person_error}")
                else:
                    # Find AU colleagues in the background (parallel ORCID checks)
                    au_colleagues: list[str] = []
                    if _PURE_AVAILABLE and coauthor_map:
                        with st.spinner(
                            "Checking co-authors for Aarhus University affiliation..."
                        ):
                            au_colleagues = find_au_colleagues(
                                coauthor_map,
                                coauthor_counts=coauthor_counts,
                                institution=institution or "Aarhus University",
                            )

                    # Build research summary from titles using AI
                    research_summary = ""
                    if titles and ai_assist:
                        with st.spinner("Summarising your research..."):
                            research_summary = _summarise_research(titles)

                    blocked_names = (
                        _group_member_names(full_name)
                        if profile_mode == "group"
                        else {full_name.strip().lower()}
                    )
                    au_colleagues = [
                        name
                        for name in au_colleagues
                        if name.strip().lower() not in blocked_names
                    ]
                    sorted_coauthors = (
                        [
                            name
                            for name, _ in sorted(
                                (coauthor_counts or {}).items(),
                                key=lambda item: (-item[1], item[0].lower()),
                            )
                            if name.strip().lower() not in blocked_names
                        ]
                        if coauthor_counts
                        else []
                    )

                    if profile_mode == "group" and any(
                        member.get("orcid_url") == orcid_url for member in group_members
                    ):
                        st.info("That ORCID is already imported for this group.")

                    st.session_state.orcid_preview = {
                        "name": full_name,
                        "institution": institution or "Aarhus University",
                        "orcid_url": orcid_url,
                        "keywords": keywords or {},
                        "titles": titles or [],
                        # Per-paper metadata (title + year) for smart pre-selection
                        "works_meta": works_meta or [],
                        "au_colleagues": au_colleagues,
                        "all_coauthors": sorted_coauthors,
                        # Raw coauthor map retained for frequency counting in suggested-colleagues
                        "coauthor_map": dict(coauthor_map) if coauthor_map else {},
                        "coauthor_counts": dict(coauthor_counts) if coauthor_counts else {},
                        "research_summary": research_summary,
                        # Track which colleagues the user wants to import
                        "selected_colleagues": list(au_colleagues),
                    }
                    if works_error:
                        st.warning(
                            "Profile found but no publications on ORCID — keywords and colleagues will be empty."
                        )

        # ── Review card: show what was found, let user correct ──
        if st.session_state.orcid_preview:
            p = st.session_state.orcid_preview
            st.markdown(f"**{p['name']}** · {p['institution']}")
            st.caption(f"ORCID: {p['orcid_url']}")

            if p["keywords"]:
                st.markdown("**Keywords from your publications:**")
                kw_display = "  ·  ".join(
                    k
                    for k, _ in sorted(p["keywords"].items(), key=lambda x: -x[1])[:12]
                )
                st.caption(kw_display + "  _(you can adjust these below)_")
            else:
                st.caption("No keywords found — you can add them manually below.")

            # ── Colleagues ──
            st.markdown(
                "**Colleagues to track** — papers by these people always appear in your digest:"
            )
            # Preserve manually added colleagues across re-renders
            p.setdefault("selected_colleagues", [])

            if p.get("au_colleagues"):
                st.caption(
                    f"Found {len(p['au_colleagues'])} co-authors at {p['institution']} — uncheck any to exclude, or add more below."
                )
                manual = [
                    c for c in p["selected_colleagues"] if c not in p["au_colleagues"]
                ]
                selected = list(manual)
                for colleague in p["au_colleagues"]:
                    checked = st.checkbox(
                        colleague, value=True, key=f"colleague_{colleague}"
                    )
                    if checked:
                        selected.append(colleague)
                p["selected_colleagues"] = selected
            else:
                if p.get("titles"):
                    st.caption(
                        f"No co-authors with confirmed {p['institution']} affiliation found automatically."
                    )

            # Show manually added colleagues (not from auto-detection)
            manual_added = [
                c
                for c in p["selected_colleagues"]
                if c not in p.get("au_colleagues", [])
            ]
            if manual_added:
                st.caption("Manually added colleagues:")
                to_remove = []
                for mc in manual_added:
                    mc_col, rm_col = st.columns([6, 1])
                    with mc_col:
                        st.markdown(f"· {mc}")
                    with rm_col:
                        if st.button("✕", key=f"rm_manual_{mc}"):
                            to_remove.append(mc)
                for mc in to_remove:
                    p["selected_colleagues"].remove(mc)
                if to_remove:
                    st.rerun()

            # Manual add by ORCID — for colleagues not found automatically
            st.caption("Add a colleague by their ORCID:")
            extra_col, extra_btn = st.columns([4, 1])
            with extra_col:
                extra_orcid = st.text_input(
                    "Colleague ORCID",
                    placeholder="0000-0001-2345-6789  or  https://orcid.org/...",
                    key="preview_extra_orcid",
                    label_visibility="collapsed",
                )
            with extra_btn:
                add_clicked = st.button("Look up", key="preview_add_colleague")

            if add_clicked and extra_orcid.strip():
                inp = extra_orcid.strip().rstrip("/")
                if inp.startswith("https://orcid.org/"):
                    lookup_id = inp.split("/")[-1]
                elif _ORCID_ID_RE.match(inp):
                    lookup_id = inp
                else:
                    st.error("Enter a valid ORCID (e.g. 0000-0001-2345-6789).")
                    lookup_id = ""

                if lookup_id and not _PURE_AVAILABLE:
                    st.info("Pure lookup is temporarily unavailable — using ORCID only.")
                elif lookup_id:
                    with st.spinner("Looking up colleague..."):
                        found_name, found_inst, found_err = fetch_orcid_person(
                            lookup_id
                        )
                    if found_err:
                        st.error(f"Could not fetch: {found_err}")
                    elif found_name and found_name not in p["selected_colleagues"]:
                        p["selected_colleagues"].append(found_name)
                        st.success(
                            f"Added {found_name} ({found_inst or 'no institution on ORCID'})"
                        )
                        st.rerun()

            # Pick from all co-authors on previous papers (already fetched from ORCID)
            all_coauthors = p.get("all_coauthors", [])
            _coauthor_blocklist = _group_member_names(p.get("name", ""))
            pickable = [
                n
                for n in all_coauthors
                if n not in p["selected_colleagues"]
                and n.strip().lower() not in _coauthor_blocklist
            ]
            if pickable:
                st.markdown(f"**Or pick from your {len(all_coauthors)} ORCID co-authors**")
                if True:
                    pick_filter = st.text_input(
                        "Filter by name",
                        key="coauthor_pick_filter",
                        placeholder="type to filter…",
                    )
                    filtered = (
                        [n for n in pickable if pick_filter.lower() in n.lower()]
                        if pick_filter
                        else pickable
                    )
                    for name in filtered[:30]:
                        if st.button(f"+ {name}", key=f"pick_coauthor_{name}"):
                            p["selected_colleagues"].append(name)
                            st.rerun()
                    if len(filtered) > 30:
                        st.caption(
                            f"Showing 30 of {len(filtered)} — type more to narrow."
                        )

            import_label = (
                "✓ Add this member"
                if profile_mode == "group"
                else "✓ Looks good — import"
            )
            if st.button(import_label, type="primary"):
                _commit_preview()
                st.rerun()

    # ── Continue button (always visible at bottom of Section 1) ──
    _s1_label = (
        "Skip ORCID — continue to Step 2 →"
        if profile_mode == "group" and not st.session_state.pure_scanned
        else "Looks good — continue to Step 2 →"
    )
    if st.button(_s1_label, key="s1_continue", type="primary"):
        st.session_state.current_step = 2
        st.rerun()


# ─────────────────────────────────────────────────────────────
#  Section 2: Your Profile
# ─────────────────────────────────────────────────────────────

with st.expander("**2. Your Profile**", expanded=(st.session_state.current_step == 2)):
    col1, col2 = st.columns(2)
    with col1:
        _name_label = "Group / course name" if profile_mode == "group" else "Your name"
        _name_placeholder = (
            "AU Exoplanet Group" if profile_mode == "group" else "Jane Smith"
        )
        researcher_name = st.text_input(
            _name_label, placeholder=_name_placeholder, key="profile_name"
        )
        institution = st.text_input(
            "Institution (optional)",
            placeholder="Aarhus University",
            key="profile_institution",
        )
    with col2:
        digest_name = st.text_input(
            "Digest name",
            value="arXiv Digest",
            help="Appears in the email subject line",
        )
        department = st.text_input(
            "Department (optional)",
            placeholder="Dept. of Physics & Astronomy",
            key="profile_department",
        )

    tagline = st.text_input(
        "Footer tagline (optional)",
        placeholder="Ad astra per aspera",
        help="A quote or motto for the email footer",
    )

    # ── Self-match (optional in group mode) ──
    if profile_mode == "group":
        st.markdown(
            "**Group members on arXiv** — optional. Add author patterns if you want the digest to flag and celebrate papers from anyone in the group."
        )
    else:
        st.markdown(
            "**Your name on arXiv** — if you publish a paper, you'll get a special celebration in your digest!"
        )
    col1, col2 = st.columns([3, 1])
    with col1:
        new_self = st.text_input(
            "Author match pattern",
            placeholder="Smith, J",
            key="self_match_input",
            label_visibility="collapsed",
            help=(
                "How a member name appears in arXiv author lists (e.g. 'Smith, J' or 'Jane Smith')"
                if profile_mode == "group"
                else "How your name appears in arXiv author lists (e.g. 'Smith, J' or 'Jane Smith')"
            ),
        )
    with col2:
        if st.button(
            "Add pattern" if profile_mode == "group" else "Add",
            key="add_self_match",
            use_container_width=True,
        ):
            if new_self.strip() and new_self.strip() not in st.session_state.self_match:
                st.session_state.self_match.append(new_self.strip())
                st.rerun()

    if st.session_state.self_match:
        to_remove = []
        for pattern in st.session_state.self_match:
            col1, col2 = st.columns([4, 1])
            with col1:
                st.markdown(f"- `{pattern}`")
            with col2:
                if st.button("✕", key=f"rm_self_{pattern}"):
                    to_remove.append(pattern)
        for p in to_remove:
            st.session_state.self_match.remove(p)
            st.rerun()

    if st.button(
        "Looks good — continue to Step 3 →", key="s2_continue", type="primary"
    ):
        st.session_state.current_step = 3
        st.rerun()

# Persist Section 2 scalar outputs so Section 10 can read them when Section 2 is collapsed
try:
    st.session_state["_s2_digest_name"] = digest_name
    st.session_state["_s2_tagline"] = tagline
except NameError:
    pass

researcher_name = st.session_state.get("profile_name", "")
institution = st.session_state.get("profile_institution", "")
department = st.session_state.get("profile_department", "")
digest_name = st.session_state.get("_s2_digest_name", "arXiv Digest")
tagline = st.session_state.get("_s2_tagline", "")


# ─────────────────────────────────────────────────────────────
#  Section 3: Research Description
# ─────────────────────────────────────────────────────────────

with st.expander(
    "**3. Your Research Description**", expanded=(st.session_state.current_step == 3)
):
    if ai_assist:
        if st.session_state.research_description:
            st.markdown(
                "Auto-drafted from your publications — edit freely. "
                "Then hit the button below to suggest categories and score your keywords."
            )
        else:
            if profile_mode == "group":
                st.markdown(
                    "Describe your group's interests in 3-5 sentences. "
                    "We'll use this to **suggest arXiv categories and score keywords** for you."
                )
            else:
                st.markdown(
                    "Describe your research in 3-5 sentences, like you'd tell a colleague. "
                    "We'll use this to **suggest arXiv categories and score keywords** for you."
                )
    else:
        st.markdown(
            "Describe your research in 3-5 sentences. This is what the AI uses to score papers."
        )

    research_context_widget = st.text_area(
        "Research context",
        value=st.session_state._research_description_val,
        height=120,
        placeholder="I study exoplanet atmospheres using transmission spectroscopy with JWST and ground-based instruments. I focus on hot Jupiters and sub-Neptunes, particularly their atmospheric composition and cloud properties.",
        label_visibility="collapsed",
        key="research_description_widget",
    )
    # Keep backing stores in sync with what the user types
    st.session_state.research_description = research_context_widget
    st.session_state._research_description_val = research_context_widget

    # ── Paper selector: choose which publications inform AI keyword/category suggestions ──
    _orcid_titles = st.session_state.get("_orcid_titles", [])
    if _orcid_titles:
        _total_papers = len(_orcid_titles)
        _smart_threshold = (
            10  # use smart pre-selection when user has this many or more papers
        )
        _paper_context_limit = 30

        def _sort_titles_by_recency(
            titles: list[str], works_meta: list[dict], cap: int = 10
        ) -> list[str]:
            """
            Return titles sorted by most recent year, capped to `cap`.

            Priority: most-recent first (year descending, None years last).
            Author-position data is not available from the ORCID summary endpoint —
            the fallback is year-descending order, which surfaces recent work reliably.
            """
            # Build a year lookup from works_meta; fall back to None when absent
            year_map: dict[str, int | None] = {}
            for entry in works_meta:
                t = entry.get("title", "")
                if t:
                    # Keep the most recent year if a title appears more than once
                    existing = year_map.get(t)
                    y = entry.get("year")
                    if existing is None or (y is not None and y > existing):
                        year_map[t] = y

            # Sort: known years descending first, unknowns at the end (preserve ORCID order)
            def _sort_key(title: str) -> tuple[int, int]:
                yr = year_map.get(title)
                return (0, -(yr or 0)) if yr is not None else (1, 0)

            candidates = sorted(titles, key=_sort_key)
            return candidates[:cap]

        # Only compute smart default once — when selected_papers is still empty (first render)
        # or when all current selections are stale (title list changed after a profile reset).
        _existing = [t for t in st.session_state.selected_papers if t in _orcid_titles]

        if not _existing:
            # First render or stale state: compute the default
            if _total_papers >= _smart_threshold:
                _works_meta = st.session_state.get("_orcid_works_meta", [])
                _default_selection = _sort_titles_by_recency(
                    _orcid_titles, _works_meta, cap=_smart_threshold
                )
                _selection_note = (
                    f"{_total_papers} papers found — showing smart selection of "
                    f"{len(_default_selection)} most recent"
                )
            else:
                _default_selection = list(_orcid_titles)
                _selection_note = ""
            _set_selected_papers(_default_selection)
        else:
            _default_selection = _existing
            _selection_note = ""
            if st.session_state.get("paper_selector_widget") != _existing:
                st.session_state["paper_selector_widget"] = list(_existing)

        st.markdown(
            "**Which papers should we use to suggest your keywords?** (select the most representative ones)"
        )
        if _total_papers >= _smart_threshold and _selection_note:
            st.caption(_selection_note)
        else:
            st.caption(
                "All fetched from your ORCID profile. Deselect papers from unrelated projects."
            )
        st.caption(
            f"AI uses at most {_paper_context_limit} selected papers for context, prioritising the most recent."
        )

        _current_selection = st.session_state.get("selected_papers", _default_selection)
        _preview_titles = _sort_titles_by_recency(
            _current_selection, st.session_state.get("_orcid_works_meta", []), cap=5
        )

        def _render_paper_selector(expanded: bool) -> list[str]:
            st.markdown(f"**Edit paper selection ({len(st.session_state.get('selected_papers', []))} selected)**")
            if True:
                quick_col1, quick_col2, quick_col3, quick_col4 = st.columns(4)
                with quick_col1:
                    if st.button("Recent 10", key="paper_recent_10"):
                        _set_selected_papers(
                            _sort_titles_by_recency(
                                _orcid_titles,
                                st.session_state.get("_orcid_works_meta", []),
                                cap=10,
                            )
                        )
                        st.rerun()
                with quick_col2:
                    if st.button("Recent 30", key="paper_recent_30"):
                        _set_selected_papers(
                            _sort_titles_by_recency(
                                _orcid_titles,
                                st.session_state.get("_orcid_works_meta", []),
                                cap=min(30, _total_papers),
                            )
                        )
                        st.rerun()
                with quick_col3:
                    if st.button("Select all", key="paper_select_all"):
                        _set_selected_papers(_orcid_titles)
                        st.rerun()
                with quick_col4:
                    if st.button("Clear", key="paper_clear"):
                        _set_selected_papers([])
                        st.rerun()

                return st.multiselect(
                    "Papers for keyword suggestions",
                    options=_orcid_titles,
                    default=_default_selection,
                    label_visibility="collapsed",
                    key="paper_selector_widget",
                )

        if _total_papers >= _smart_threshold:
            st.caption(
                f"Currently using {len(_current_selection)} paper"
                f"{'' if len(_current_selection) == 1 else 's'}."
            )
            if _preview_titles:
                for preview_title in _preview_titles:
                    st.caption(f"• {preview_title}")
            _new_selection = _render_paper_selector(expanded=False)
        else:
            _new_selection = _render_paper_selector(expanded=True)

        st.session_state.selected_papers = _new_selection
        if len(_new_selection) < _total_papers:
            st.caption(f"{len(_new_selection)} of {_total_papers} papers selected.")

    # ── AI suggestions: auto-run if description was auto-drafted, else show button ──
    if ai_assist and research_context_widget and len(research_context_widget) > 30:
        _has_orcid_kws = bool(st.session_state.keywords)
        _api_available = _ai_available()
        _cats_already_suggested = bool(st.session_state.ai_suggested_cats)

        # Auto-trigger when profile was imported and description was drafted automatically
        _auto_trigger = st.session_state.pure_scanned and not _cats_already_suggested

        # Build enriched context: research description + selected paper titles (if any)
        _selected_titles = st.session_state.get("selected_papers", [])
        _works_meta = st.session_state.get("_orcid_works_meta", [])
        _effective_titles = _sort_titles_by_recency(
            _selected_titles, _works_meta, cap=_paper_context_limit
        )
        if len(_selected_titles) > _paper_context_limit:
            st.warning(
                f"You selected {len(_selected_titles)} papers. AI will use the {_paper_context_limit} most recent selected papers."
            )
        if _effective_titles:
            _titles_block = "\n".join(f"- {t}" for t in _effective_titles)
            _ai_context = (
                research_context_widget
                + f"\n\nRepresentative publications:\n{_titles_block}"
            )
        else:
            _ai_context = research_context_widget

        if _auto_trigger:
            with st.spinner("Suggesting categories and scoring keywords..."):
                st.session_state.ai_suggested_cats = suggest_categories(_ai_context)
                st.session_state.ai_suggested_kws = suggest_keywords_from_context(
                    _ai_context,
                    orcid_keywords=st.session_state.keywords
                    if _has_orcid_kws
                    else None,
                )
                if _api_available and _has_orcid_kws:
                    # Merge: keep all existing keywords; update scores where AI returned one.
                    # Case-insensitive match: build a lowercased lookup from AI results.
                    ai_lower = {
                        k.lower(): v
                        for k, v in st.session_state.ai_suggested_kws.items()
                    }
                    merged_kws = dict(st.session_state.keywords)
                    for kw in list(merged_kws.keys()):
                        ai_score = ai_lower.get(kw.lower())
                        if ai_score is not None:
                            merged_kws[kw] = ai_score
                    st.session_state.keywords = merged_kws
            st.rerun()
        else:
            _btn_label = (
                "🤖 Re-score categories & keywords"
                if _cats_already_suggested
                else (
                    "🤖 Suggest categories & score keywords"
                    if _api_available
                    else "🤖 Suggest categories & keywords"
                )
            )
            if st.button(
                _btn_label, type="secondary" if _cats_already_suggested else "primary"
            ):
                st.session_state.ai_suggested_cats = suggest_categories(_ai_context)
                st.session_state.ai_suggested_kws = suggest_keywords_from_context(
                    _ai_context,
                    orcid_keywords=st.session_state.keywords
                    if _has_orcid_kws
                    else None,
                )
                if _api_available and _has_orcid_kws:
                    # Merge: keep all existing keywords; update scores where AI returned one.
                    # Case-insensitive match: build a lowercased lookup from AI results.
                    ai_lower = {
                        k.lower(): v
                        for k, v in st.session_state.ai_suggested_kws.items()
                    }
                    merged_kws = dict(st.session_state.keywords)
                    for kw in list(merged_kws.keys()):
                        ai_score = ai_lower.get(kw.lower())
                        if ai_score is not None:
                            merged_kws[kw] = ai_score
                    st.session_state.keywords = merged_kws

        if st.session_state.ai_suggested_cats:
            st.success(
                f"Suggested {len(st.session_state.ai_suggested_cats)} categories and {len(st.session_state.ai_suggested_kws)} keywords — review them below."
            )

    if st.button(
        "Looks good — continue to Step 4 →", key="s3_continue", type="primary"
    ):
        st.session_state.current_step = 4
        st.rerun()

# research_context is used in Section 10 config dict — read from backing store
research_context = st.session_state.get("research_description", "")


# ─────────────────────────────────────────────────────────────
#  Section 4: arXiv Categories
# ─────────────────────────────────────────────────────────────

# Build set of AI-suggested categories for pre-selection.
# Computed outside the expander so categories variable is available for Section 10
# regardless of whether Section 4 is currently open.
ai_suggested_set = set(st.session_state.ai_suggested_cats) if ai_assist else set()

# Track which categories the user has selected across all groups
if "selected_categories" not in st.session_state:
    st.session_state.selected_categories = set(ai_suggested_set)

# If AI suggestions just arrived, merge them into the selection
if ai_suggested_set and not st.session_state.selected_categories.issuperset(
    ai_suggested_set
):
    st.session_state.selected_categories.update(ai_suggested_set)

with st.expander(
    "**4. arXiv Categories**", expanded=(st.session_state.current_step == 4)
):
    if ai_assist and ai_suggested_set:
        st.success(
            f"AI suggested {len(ai_suggested_set)} categories based on your research description. "
            f"They are pre-selected below — review and adjust as needed."
        )

    st.markdown(
        "Pick the arXiv groups you want to monitor, then choose sub-categories within each group. "
        "Each group header shows a hint for when to include it."
    )

    # ── Group-level hierarchical picker ──
    to_add = set()
    to_remove = set()

    for group_name, group_cats in ARXIV_GROUPS.items():
        selected_in_group = [
            c for c in group_cats if c in st.session_state.selected_categories
        ]
        n_selected = len(selected_in_group)
        n_total = len(group_cats)
        hint = ARXIV_GROUP_HINTS.get(group_name, "")

        count_label = f"{n_selected}/{n_total} selected" if n_selected > 0 else ""
        show_group = st.checkbox(
            f"**{group_name}**" + (f" — {count_label}" if count_label else ""),
            value=(n_selected > 0),
            key=f"grp_show_{group_name}",
        )
        if show_group:
            if hint:
                st.caption(f"Include if: {hint}")

            col_all, col_none, col_spacer = st.columns([1, 1, 4])
            with col_all:
                if st.button(
                    "Select all", key=f"grp_all_{group_name}", use_container_width=True
                ):
                    to_add.update(group_cats)
            with col_none:
                if st.button(
                    "Clear", key=f"grp_none_{group_name}", use_container_width=True
                ):
                    to_remove.update(group_cats)

            for cat_id in group_cats:
                label = ARXIV_CATEGORIES.get(cat_id, cat_id)
                is_selected = cat_id in st.session_state.selected_categories
                # ✦ marks AI-suggested categories (Unicode, not an emoji)
                display_label = (
                    f"{label} \u2726" if cat_id in ai_suggested_set else label
                )
                checked = st.checkbox(
                    display_label,
                    value=is_selected,
                    key=f"cat_{cat_id}",
                    help=f"`{cat_id}`"
                    + (" — AI suggested" if cat_id in ai_suggested_set else ""),
                )
                if checked and not is_selected:
                    to_add.add(cat_id)
                elif not checked and is_selected:
                    to_remove.add(cat_id)

    # Apply batch updates after the loop (avoids mid-loop state mutations)
    if to_add or to_remove:
        st.session_state.selected_categories = (
            st.session_state.selected_categories | to_add
        ) - to_remove
        st.rerun()

    # Summary of selected categories
    _cats_now = sorted(st.session_state.selected_categories)
    if _cats_now:
        st.markdown(
            f"**{len(_cats_now)} categories selected:** "
            + ", ".join(f"`{c}`" for c in _cats_now)
        )
    else:
        st.info("No categories selected yet. Expand a group above to choose.")

    if st.button(
        "Looks good — continue to Step 5 →", key="s4_continue", type="primary"
    ):
        st.session_state.current_step = 5
        st.rerun()

# categories must be available outside the expander for the config dict in Section 10
categories = sorted(st.session_state.selected_categories)


# ─────────────────────────────────────────────────────────────
#  Section 5: Keywords
# ─────────────────────────────────────────────────────────────

with st.expander("**5. Keywords**", expanded=(st.session_state.current_step == 5)):
    st.markdown(
        "Papers matching these keywords get pre-filtered before AI scoring. Higher weight = more important."
    )

    # If AI suggested keywords, offer to add them
    if ai_assist and st.session_state.ai_suggested_kws:
        new_suggestions = {
            k: v
            for k, v in st.session_state.ai_suggested_kws.items()
            if k not in st.session_state.keywords
        }
        if new_suggestions:
            st.markdown("**Suggested keywords** — click to add:")
            cols = st.columns(3)
            to_add = {}
            for i, (kw, weight) in enumerate(new_suggestions.items()):
                with cols[i % 3]:
                    if st.button(
                        f"+ {kw} ({weight})",
                        key=f"add_sug_{kw}",
                        use_container_width=True,
                    ):
                        to_add[kw] = weight
            if to_add:
                st.session_state.keywords.update(to_add)
                st.rerun()

            if st.button("Add all suggested keywords"):
                st.session_state.keywords.update(new_suggestions)
                st.rerun()

    # Manual keyword entry
    st.markdown("**Add keyword manually:**")
    col1, col2, col3 = st.columns([3, 2, 1])
    with col1:
        new_kw = st.text_input(
            "Keyword",
            placeholder="transmission spectroscopy",
            label_visibility="collapsed",
            key="new_kw_input",
        )
    with col2:
        new_weight = st.slider(
            "Weight", 1, 10, 7, label_visibility="collapsed", key="new_kw_weight"
        )
        st.caption(f"_{_weight_label(new_weight)}_")
    with col3:
        if st.button("Add", use_container_width=True, key="add_kw_btn"):
            if new_kw.strip():
                st.session_state.keywords[new_kw.strip()] = new_weight
                st.rerun()

    # Display existing keywords with editable weight sliders
    if st.session_state.keywords:
        st.markdown("**Your keywords:**")
        _kw_col_all, _kw_col_clear, _kw_col_spacer = st.columns([1, 1, 4])
        with _kw_col_all:
            if st.button(
                "Select all",
                key="kw_select_all",
                use_container_width=True,
                help="Set all keyword weights to 10",
            ):
                st.session_state.keywords = {k: 10 for k in st.session_state.keywords}
                st.rerun()
        with _kw_col_clear:
            if st.button(
                "Clear all",
                key="kw_clear_all",
                use_container_width=True,
                help="Set all keyword weights to 1",
            ):
                st.session_state.keywords = {k: 1 for k in st.session_state.keywords}
                st.rerun()
        to_remove = []
        for kw, weight in sorted(
            st.session_state.keywords.items(), key=lambda x: -x[1]
        ):
            col1, col2, col3 = st.columns([3, 2, 1])
            with col1:
                st.markdown(f"`{kw}`")
            with col2:
                new_w = st.slider(
                    "weight",
                    1,
                    10,
                    weight,
                    key=f"kw_slider_{kw}",
                    label_visibility="collapsed",
                )
                st.caption(f"_{_weight_label(new_w)}_")
                # Update weight in-place — no rerun needed, slider state persists
                st.session_state.keywords[kw] = new_w
            with col3:
                if st.button("✕", key=f"rm_kw_{kw}", help=f"Remove {kw}"):
                    to_remove.append(kw)
        for kw in to_remove:
            del st.session_state.keywords[kw]
            st.rerun()
    else:
        st.info(
            "No keywords yet. Add some above, scan your Pure profile, or use AI suggestions."
        )

    if st.button(
        "Looks good — continue to Step 6 →", key="s5_continue", type="primary"
    ):
        st.session_state.current_step = 6
        st.rerun()


# ─────────────────────────────────────────────────────────────
#  Section 6: Research Authors
# ─────────────────────────────────────────────────────────────

with st.expander(
    "**6. Research Authors**", expanded=(st.session_state.current_step == 6)
):
    if profile_mode == "group":
        st.markdown(
            "Papers by these people get a relevance boost. Useful for your lab, reading group, or the researchers your group follows most closely."
        )
    else:
        st.markdown(
            "Papers by these people get a relevance boost. Use partial name strings (e.g. 'Madhusudhan')."
        )

    new_author = st.text_input(
        "Add research author", placeholder="Madhusudhan", key="new_ra_input"
    )
    if st.button("Add author") and new_author.strip():
        if new_author.strip() not in st.session_state.research_authors:
            st.session_state.research_authors.append(new_author.strip())
            st.rerun()

    if st.session_state.research_authors:
        to_remove = []
        for author in st.session_state.research_authors:
            col1, col2 = st.columns([4, 1])
            with col1:
                st.markdown(f"- {author}")
            with col2:
                if st.button("✕", key=f"rm_ra_{author}"):
                    to_remove.append(author)
        for a in to_remove:
            st.session_state.research_authors.remove(a)
            st.rerun()

    if st.button(
        "Looks good — continue to Step 7 →", key="s6_continue", type="primary"
    ):
        st.session_state.current_step = 7
        st.rerun()


# ─────────────────────────────────────────────────────────────
#  Section 7: Colleagues
# ─────────────────────────────────────────────────────────────

with st.expander("**7. Colleagues**", expanded=(st.session_state.current_step == 7)):
    st.markdown(
        "Papers by colleagues always appear in a special section, even if off-topic. Great for staying social!"
    )

    st.markdown("**People:**")
    col1, col2 = st.columns([2, 2])
    with col1:
        new_coll_name = st.text_input(
            "Colleague name", placeholder="Jane Smith", key="new_coll_name"
        )
    with col2:
        new_coll_match = st.text_input(
            "Match pattern",
            placeholder="Smith, J",
            key="new_coll_match",
            help="How their name appears in arXiv author lists",
        )

    if st.button("Add colleague") and new_coll_name.strip() and new_coll_match.strip():
        st.session_state.colleagues_people.append(
            {
                "name": new_coll_name.strip(),
                "match": [new_coll_match.strip()],
            }
        )
        st.rerun()

    if st.session_state.colleagues_people:
        to_remove = []
        for i, coll in enumerate(st.session_state.colleagues_people):
            col1, col2, col3 = st.columns([2, 2, 1])
            with col1:
                st.markdown(f"**{coll['name']}**")
            with col2:
                st.markdown(f"match: `{', '.join(coll['match'])}`")
            with col3:
                if st.button("✕", key=f"rm_coll_{i}"):
                    to_remove.append(i)
        for idx in sorted(to_remove, reverse=True):
            st.session_state.colleagues_people.pop(idx)
        if to_remove:
            st.rerun()

    # ── Suggested colleagues — top co-authors from ORCID publications ──
    _coauthor_counts = st.session_state.get("_orcid_coauthor_counts", {})
    if _coauthor_counts:
        _name_freq = dict(_coauthor_counts)
        # Remove the user themselves by checking against their profile name
        _user_name = st.session_state.get("profile_name", "").strip()
        _user_name_lower = _user_name.lower() if _user_name else ""
        _group_member_blocklist = _group_member_names()
        # Exclude names already in colleagues_people
        _already_tracked = {c["name"] for c in st.session_state.colleagues_people}
        _candidates = [
            (name, count)
            for name, count in sorted(
                _name_freq.items(), key=lambda item: (-item[1], item[0].lower())
            )[:20]
            if name not in _already_tracked
            and name.strip()
            and count >= 2
            and name.lower() != _user_name_lower
            and name.lower() not in _group_member_blocklist
        ]
        _top_coauthors = _candidates[:5]

        if len(_top_coauthors) >= 2:
            st.markdown(
                "**Suggested colleagues** — based on your most frequent co-authors:"
            )
            st.caption("One-click to add them to your People to Track list.")
            for _ca_name, _ca_count in _top_coauthors:
                _shared_label = f"{'paper' if _ca_count == 1 else 'papers'}"
                _col_name, _col_count, _col_btn = st.columns([4, 2, 2])
                with _col_name:
                    st.markdown(f"**{_ca_name}**")
                with _col_count:
                    st.caption(f"{_ca_count} shared {_shared_label}")
                with _col_btn:
                    if st.button(
                        "+ Add",
                        key=f"suggest_coll_{_ca_name}",
                        use_container_width=True,
                    ):
                        _parts = _ca_name.split()
                        _match_pat = (
                            f"{_parts[-1]}, {_parts[0][0]}"
                            if len(_parts) >= 2
                            else _ca_name
                        )
                        st.session_state.colleagues_people.append(
                            {
                                "name": _ca_name,
                                "match": [_match_pat],
                            }
                        )
                        st.rerun()

    st.markdown("**Institutions** (match against abstract text):")
    new_inst = st.text_input(
        "Add institution", placeholder="Aarhus University", key="new_inst_input"
    )
    if st.button("Add institution") and new_inst.strip():
        if new_inst.strip() not in st.session_state.colleagues_institutions:
            st.session_state.colleagues_institutions.append(new_inst.strip())
            st.rerun()

    if st.session_state.colleagues_institutions:
        to_remove = []
        for inst in st.session_state.colleagues_institutions:
            col1, col2 = st.columns([4, 1])
            with col1:
                st.markdown(f"- {inst}")
            with col2:
                if st.button("✕", key=f"rm_inst_{inst}"):
                    to_remove.append(inst)
        for inst in to_remove:
            st.session_state.colleagues_institutions.remove(inst)
        if to_remove:
            st.rerun()

    if st.button(
        "Looks good — continue to Step 8 →", key="s7_continue", type="primary"
    ):
        st.session_state.current_step = 8
        st.rerun()


# ─────────────────────────────────────────────────────────────
#  Section 8: Digest Mode & Schedule
# ─────────────────────────────────────────────────────────────

with st.expander(
    "**8. Digest Mode & Schedule**", expanded=(st.session_state.current_step == 8)
):
    # ── Digest mode ──
    st.markdown("**How much do you want to read?**")
    digest_mode = st.radio(
        "Digest mode",
        options=["highlights", "in_depth"],
        format_func=lambda x: {
            "highlights": "🎯 Highlights — just the top papers (fewer, higher quality)",
            "in_depth": "📚 In-depth — wider net, more papers to browse",
        }[x],
        horizontal=True,
        label_visibility="collapsed",
    )

    # Show what the mode means
    if digest_mode == "highlights":
        st.caption(
            "Default: up to 6 papers, min score 5/10. Only the most relevant papers make it through."
        )
    else:
        st.caption(
            "Default: up to 15 papers, min score 2/10. Casts a wider net — great for staying broadly informed."
        )

    st.markdown("**Recipient email view**")
    recipient_view_mode = st.radio(
        "Recipient email view",
        options=["deep_read", "5_min_skim"],
        format_func=lambda x: {
            "deep_read": "📖 Deep read — full cards with expanded context",
            "5_min_skim": "⚡ 5-minute skim — top 3 papers, one-line summaries",
        }[x],
        horizontal=True,
        label_visibility="collapsed",
    )

    # ── Advanced overrides ──
    mode_defaults = {"highlights": (6, 5), "in_depth": (15, 2)}
    default_max, default_min = mode_defaults[digest_mode]
    override_max = False
    override_min = False

    st.markdown("**Fine-tune (optional)**")
    if True:
        col1, col2 = st.columns(2)
        with col1:
            max_papers = st.number_input(
                "Max papers per digest", min_value=1, max_value=30, value=default_max
            )
        with col2:
            min_score = st.number_input(
                "Min relevance score (1-10)",
                min_value=1,
                max_value=10,
                value=default_min,
            )

        override_max = max_papers != default_max
        override_min = min_score != default_min

    st.markdown("---")

    # ── Schedule ──
    st.markdown("**How often should the digest arrive?**")
    schedule_options = {
        "mon_wed_fri": "Mon / Wed / Fri",
        "daily": "Every weekday (Mon–Fri)",
        "weekly": "Once a week (Monday)",
    }
    schedule = st.radio(
        "Frequency",
        options=list(schedule_options.keys()),
        format_func=lambda x: schedule_options[x],
        horizontal=True,
        label_visibility="collapsed",
    )

    # ── Days back (auto-set based on schedule, with override) ──
    schedule_days_back = {"daily": 2, "mon_wed_fri": 4, "weekly": 8}
    days_back = schedule_days_back[schedule]

    override_days = st.checkbox("Override days back", value=False, key="override_days_back")
    if override_days:
        days_back = st.number_input(
            "Days to look back", min_value=1, max_value=14, value=days_back
        )

    st.caption(f"Will look back **{days_back} days** for new papers.")

    # ── Send time ──
    st.markdown("**What time should it arrive?** (UTC)")
    send_hour_utc = st.slider(
        "Send hour (UTC)",
        min_value=0,
        max_value=23,
        value=7,
        help="Default is 7 UTC = 9am Danish time (CET). Adjust for your timezone.",
        label_visibility="collapsed",
    )

    # Show common timezone equivalents
    tz_examples = []
    if 0 <= send_hour_utc <= 23:
        cet = (send_hour_utc + 1) % 24
        cest = (send_hour_utc + 2) % 24
        est = (send_hour_utc - 5) % 24
        pst = (send_hour_utc - 8) % 24
        tz_examples = [
            f"CET: {cet}:00",
            f"CEST: {cest}:00",
            f"EST: {est}:00",
            f"PST: {pst}:00",
        ]
    st.caption(" · ".join(tz_examples))

    # ── Generate cron expression ──
    CRON_MAP = {
        "daily": f"0 {send_hour_utc} * * 1-5",
        "mon_wed_fri": f"0 {send_hour_utc} * * 1,3,5",
        "weekly": f"0 {send_hour_utc} * * 1",
    }
    cron_expr = CRON_MAP[schedule]

    # Persist so Section 10 can read these when Section 8 is collapsed
    st.session_state["_s8_digest_mode"] = digest_mode
    st.session_state["_s8_schedule"] = schedule
    st.session_state["_s8_schedule_options"] = schedule_options
    st.session_state["_s8_send_hour_utc"] = send_hour_utc
    st.session_state["_s8_days_back"] = days_back
    st.session_state["_s8_cron_expr"] = cron_expr
    st.session_state["_s8_override_max"] = override_max
    st.session_state["_s8_override_min"] = override_min
    st.session_state["_s8_max_papers"] = max_papers
    st.session_state["_s8_min_score"] = min_score
    st.session_state["_s8_recipient_view_mode"] = recipient_view_mode

    if st.button(
        "Looks good — continue to Step 9 →", key="s8_continue", type="primary"
    ):
        st.session_state.current_step = 9
        st.rerun()

# Read Section 8 outputs — valid whether the expander is open or collapsed
digest_mode = st.session_state.get("_s8_digest_mode", "highlights")
schedule = st.session_state.get("_s8_schedule", "mon_wed_fri")
schedule_options = st.session_state.get(
    "_s8_schedule_options",
    {
        "mon_wed_fri": "Mon / Wed / Fri",
        "daily": "Every weekday (Mon–Fri)",
        "weekly": "Once a week (Monday)",
    },
)
send_hour_utc = st.session_state.get("_s8_send_hour_utc", 7)
days_back = st.session_state.get("_s8_days_back", 4)
cron_expr = st.session_state.get("_s8_cron_expr", f"0 7 * * 1,3,5")
override_max = st.session_state.get("_s8_override_max", False)
override_min = st.session_state.get("_s8_override_min", False)
_def_max, _def_min = {"highlights": (6, 5), "in_depth": (15, 2)}.get(
    digest_mode, (6, 5)
)
max_papers = st.session_state.get("_s8_max_papers", _def_max)
min_score_val = st.session_state.get("_s8_min_score", _def_min)
recipient_view_mode = st.session_state.get("_s8_recipient_view_mode", "deep_read")


# ─────────────────────────────────────────────────────────────
#  Section 9: Email Provider
# ─────────────────────────────────────────────────────────────

with st.expander(
    "**9. Email Provider**", expanded=(st.session_state.current_step == 9)
):
    smtp_options = {
        "Gmail": ("smtp.gmail.com", 587),
        "Outlook / Office 365": ("smtp.office365.com", 587),
    }
    smtp_choice = st.radio(
        "SMTP provider",
        options=list(smtp_options.keys()),
        horizontal=True,
        label_visibility="collapsed",
    )
    smtp_server, smtp_port = smtp_options[smtp_choice]

    github_repo = st.text_input(
        "GitHub repo (optional)",
        placeholder="username/arxiv-digest",
        help="Enables self-service links in emails",
    )

    # Persist so Section 10 can read when Section 9 is collapsed
    st.session_state["_s9_smtp_server"] = smtp_server
    st.session_state["_s9_smtp_port"] = smtp_port
    st.session_state["_s9_github_repo"] = github_repo

    if st.button(
        "Looks good — continue to Step 10 →", key="s9_continue", type="primary"
    ):
        st.session_state.current_step = 10
        st.rerun()

# Read Section 9 outputs — valid whether open or collapsed
smtp_server = st.session_state.get("_s9_smtp_server", "smtp.gmail.com")
smtp_port = st.session_state.get("_s9_smtp_port", 587)
github_repo = st.session_state.get("_s9_github_repo", "")


# ─────────────────────────────────────────────────────────────
#  Section 10: Preview & Download
# ─────────────────────────────────────────────────────────────

with st.expander(
    "**10. Preview & Download**", expanded=(st.session_state.current_step == 10)
):
    st.markdown("### Your config.yaml is ready")

    # Build config dict
    config = {
        "digest_name": digest_name or "arXiv Digest",
        "researcher_name": researcher_name
        or ("Research Group" if profile_mode == "group" else "Reader"),
        "research_context": research_context or "",
        "categories": categories
        if categories
        else [
            "astro-ph.EP",
            "astro-ph.SR",
            "astro-ph.GA",
            "astro-ph.HE",
            "astro-ph.IM",
        ],
        "keywords": dict(st.session_state.keywords)
        if st.session_state.keywords
        else {"example keyword": 5},
        "self_match": list(st.session_state.self_match),
        "research_authors": list(st.session_state.research_authors),
        "colleagues": {
            "people": list(st.session_state.colleagues_people),
            "institutions": list(st.session_state.colleagues_institutions),
        },
        "digest_mode": digest_mode,
        "recipient_view_mode": recipient_view_mode,
        "days_back": days_back,
        "schedule": schedule,
        "send_hour_utc": send_hour_utc,
        "institution": institution or "",
        "department": department or "",
        "tagline": tagline or "",
        "smtp_server": smtp_server,
        "smtp_port": smtp_port,
        "github_repo": github_repo or "",
    }

    # Only include overrides if user changed them from mode defaults
    if override_max:
        config["max_papers"] = max_papers
    if override_min:
        config["min_score"] = min_score_val
    if profile_mode == "group" and st.session_state.get("group_orcid_members"):
        config["group_members"] = [
            {
                "name": member.get("name", ""),
                "institution": member.get("institution", ""),
                "orcid_url": member.get("orcid_url", ""),
            }
            for member in st.session_state.group_orcid_members
        ]

    config_yaml = yaml.dump(
        config, default_flow_style=False, sort_keys=False, allow_unicode=True
    )

    tab1, tab2 = st.tabs(["config.yaml", "Workflow cron"])

    with tab1:
        st.code(config_yaml, language="yaml")

    with tab2:
        st.markdown(
            "If you change the schedule from the default (Mon/Wed/Fri 7am UTC), update this line in `.github/workflows/digest.yml`:"
        )
        st.code(
            f"    - cron: '{cron_expr}'  # {schedule_options.get(schedule, schedule)} at {send_hour_utc}:00 UTC",
            language="yaml",
        )
        if schedule != "mon_wed_fri" or send_hour_utc != 7:
            st.warning(
                "Your schedule differs from the default. Remember to update the cron line in your workflow file after forking!"
            )

    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            label="📥 Download config.yaml",
            data=config_yaml,
            file_name="config.yaml",
            mime="text/yaml",
            type="primary",
            use_container_width=True,
        )
    with col2:
        if st.button("📋 Show YAML", use_container_width=True):
            st.code(config_yaml, language="yaml")
            st.info("Select all text above and copy with Ctrl+C (Cmd+C on Mac)")


# ─────────────────────────────────────────────────────────────
#  Next Steps (always visible — outside accordion)
# ─────────────────────────────────────────────────────────────

st.divider()

st.markdown("## Next Steps")

# Custom schedule note
schedule_note = ""
if schedule != "mon_wed_fri" or send_hour_utc != 7:
    schedule_note = f"""
<div class="brand-card" style="border-left: 4px solid {GOLD};">
<p>⚠️ <strong>Update your schedule</strong></p>
<p style="margin-left: 36px;">
Since you chose <strong>{schedule_options.get(schedule, schedule)} at {send_hour_utc}:00 UTC</strong>, open
<code>.github/workflows/digest.yml</code> in your fork and change the cron line to:<br>
<code>- cron: '{cron_expr}'</code>
</p>
</div>
"""

st.markdown(
    f"""
<div class="brand-card">
<p><span class="step-number">1</span> <strong>Fork the template repo</strong></p>
<p style="margin-left: 36px;">
Go to <a href="https://github.com/SilkeDainese/arxiv-digest" style="color: {PINE};">github.com/SilkeDainese/arxiv-digest</a>
and click <strong>Fork</strong>.
</p>
</div>

<div class="brand-card">
<p><span class="step-number">2</span> <strong>Upload your config.yaml</strong></p>
<p style="margin-left: 36px;">
In your fork, click <strong>Add file → Upload files</strong> and upload the <code>config.yaml</code>
you just downloaded. It will replace the example config.
</p>
</div>

<div class="brand-card">
<p><span class="step-number">3</span> <strong>Add secrets to your fork</strong></p>
<p style="margin-left: 36px;">
Go to your fork's <strong>Settings → Secrets and variables → Actions</strong> and add the secrets shown below.
</p>
</div>

{schedule_note}
""",
    unsafe_allow_html=True,
)

_server_gemini = _has_server_key()
_invite_bundle = st.session_state.get("_invite_bundle", {})

st.markdown(
    "**Add these secrets to your fork** (Settings → Secrets and variables → Actions):"
)
if _invite_bundle:
    st.markdown("**Easy mode — your invite code unlocked shared secrets**")
    _shared_secret_lines = [
        "RECIPIENT_EMAIL    = your-email@example.com  ← or alice@example.com, bob@example.com"
    ]
    if _invite_bundle.get("relay_token"):
        _shared_secret_lines.append(
            f"DIGEST_RELAY_TOKEN = {_invite_bundle['relay_token']}"
        )
    if _invite_bundle.get("gemini_api_key"):
        _shared_secret_lines.append(
            f"GEMINI_API_KEY    = {_invite_bundle['gemini_api_key']}"
        )
    if _invite_bundle.get("anthropic_api_key"):
        _shared_secret_lines.append(
            f"ANTHROPIC_API_KEY = {_invite_bundle['anthropic_api_key']}"
        )
    st.code("\n".join(_shared_secret_lines), language="ini")
    st.caption(
        "Paste these into your fork's GitHub Actions secrets. Do not commit them to the repo."
    )
else:
    st.markdown("**Option A — maintainer-managed relay**")
    st.code(
        "RECIPIENT_EMAIL    = your-email@example.com  ← or alice@example.com, bob@example.com\n"
        "DIGEST_RELAY_TOKEN = paste-token-here        ← only if the maintainer gave you one",
        language="ini",
    )
    st.caption("Use this option only if you were given a private relay token.")

    st.markdown("**Option B — send from your own mailbox**")
    st.code(
        "RECIPIENT_EMAIL = your-email@example.com  ← or alice@example.com, bob@example.com\n"
        "SMTP_USER       = your-gmail@gmail.com\n"
        "SMTP_PASSWORD   = your-app-password  ← Gmail App Password, not your login password",
        language="ini",
    )
    st.caption(
        "Gmail app password: Google Account → Security → 2-Step Verification → "
        "[App passwords](https://myaccount.google.com/apppasswords)"
    )

    if not _server_gemini:
        st.markdown("**Optional AI key for better scoring**")
        st.code(
            "GEMINI_API_KEY    = AIza...      ← free at aistudio.google.com\n"
            "ANTHROPIC_API_KEY = sk-ant-...   ← optional alternative",
            language="ini",
        )

st.success(
    f"That's it! Your digest will run {schedule_options.get(schedule, schedule).lower()} at {send_hour_utc}:00 UTC. 🎉"
)

st.divider()

# ── Footer ──
st.markdown(
    f"""
<div style="text-align: center; font-family: 'DM Mono', monospace; font-size: 10px;
     letter-spacing: 0.1em; color: {WARM_GREY}; margin-top: 24px; margin-bottom: 24px;">
     Built by <a href="https://silkedainese.github.io" style="color: {PINE};">Silke S. Dainese</a> ·
     <a href="mailto:dainese@phys.au.dk" style="color: {WARM_GREY};">dainese@phys.au.dk</a> ·
     <a href="https://github.com/SilkeDainese" style="color: {WARM_GREY};">GitHub</a>
</div>
""",
    unsafe_allow_html=True,
)
