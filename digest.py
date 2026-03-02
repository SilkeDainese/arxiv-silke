"""
Science News for Silke 🔭
Fetches new arXiv papers, curates them with Claude, and sends a beautiful HTML digest.
"""

import os
import json
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import anthropic

# ─────────────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────────────

CONFIG = {
    "categories": ["astro-ph.EP", "astro-ph.SR", "astro-ph.GA"],
    "keywords": [
        "circumbinary", "circumbinary planet", "Gaia", "vbroad", "vsini",
        "stellar rotation", "binary star", "exoplanet demographics",
        "orbital architecture", "Gaia DR4", "spectroscopic binary",
        "eclipsing binary", "macroturbulence", "LAMOST", "rotation velocity",
    ],
    "known_authors": [
        "REDACTED", "Albrecht, S",
        "REDACTED", "REDACTED", "REDACTED", "REDACTED",
        "REDACTED", "Nielsen",
    ],
    "days_back": 5,
    "max_papers": 8,
    "min_score": 4,
    "recipient_email": os.environ.get("RECIPIENT_EMAIL", ""),
}


Silke is a REDACTED,
REDACTED:
- REDACTED data
- REDACTED measurements
  (using NOT and LAMOST datasets)
- Stellar rotation velocities in the Teff 6000-8000 K range
- Forward-modelling Gaia broadening measurements (rotation + macroturbulence + instrumental floor ~5 km/s)
"""

# ─────────────────────────────────────────────────────────────
#  ARXIV FETCHING
# ─────────────────────────────────────────────────────────────

def fetch_arxiv_papers(categories, days_back):
    papers = []
    for category in categories:
        params = {
            "search_query": f"cat:{category}",
            "start": 0,
            "max_results": 100,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        url = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
        print(f"  Fetching {category}...")
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                xml_data = response.read().decode("utf-8")
        except Exception as e:
            print(f"  Error: {e}")
            continue

        root = ET.fromstring(xml_data)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        cutoff = datetime.utcnow() - timedelta(days=days_back)

        for entry in root.findall("atom:entry", ns):
            published_str = entry.find("atom:published", ns).text
            published = datetime.fromisoformat(published_str.replace("Z", "+00:00")).replace(tzinfo=None)
            if published < cutoff:
                continue

            arxiv_id = entry.find("atom:id", ns).text.split("/abs/")[-1]
            title = entry.find("atom:title", ns).text.strip().replace("\n", " ")
            abstract = entry.find("atom:summary", ns).text.strip().replace("\n", " ")
            authors = [a.find("atom:name", ns).text for a in entry.findall("atom:author", ns)]

            known_flag = []
            for author in authors:
                for known in CONFIG["known_authors"]:
                    if known.lower() in author.lower():
                        known_flag.append(author)
                        break

            text_lower = (title + " " + abstract).lower()
            kw_hits = sum(1 for kw in CONFIG["keywords"] if kw.lower() in text_lower)

            papers.append({
                "id": arxiv_id,
                "title": title,
                "abstract": abstract,
                "authors": authors,
                "published": published.strftime("%Y-%m-%d"),
                "category": category,
                "url": f"https://arxiv.org/abs/{arxiv_id}",
                "known_authors": known_flag,
                "keyword_hits": kw_hits,
            })

    seen = set()
    unique = []
    for p in papers:
        if p["id"] not in seen:
            seen.add(p["id"])
            unique.append(p)

    print(f"  Found {len(unique)} papers total")
    return unique


def pre_filter(papers):
    filtered = [p for p in papers if p["keyword_hits"] > 0 or p["known_authors"]]
    filtered.sort(key=lambda p: (len(p["known_authors"]) * 5 + p["keyword_hits"]), reverse=True)
    return filtered[:30]


# ─────────────────────────────────────────────────────────────
#  CLAUDE ANALYSIS
# ─────────────────────────────────────────────────────────────

def analyse_papers(papers):
    if not papers:
        return []

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    analysed = []

    for i, paper in enumerate(papers):
        print(f"  Analysing {i+1}/{len(papers)}: {paper['title'][:60]}...")

        prompt = f"""You are helping curate a personalised arXiv digest for an astronomer.

RESEARCHER CONTEXT:
{SILKE_CONTEXT}

PAPER:
Title: {paper['title']}
Authors: {', '.join(paper['authors'][:8])}
Category: {paper['category']}
Abstract: {paper['abstract']}

Respond with ONLY a valid JSON object (no markdown, no backticks):
{{
  "relevance_score": <integer 1-10>,
  "plain_summary": "<2-3 sentences explaining what they did, like explaining to a smart friend at a pub>",
  "why_interesting": "<1-2 sentences on why specifically relevant to Silke's work>",
  "emoji": "<one relevant emoji>",
  "highlight_phrase": "<punchy 5-8 word headline>",
  "kw_tags": ["<1-3 short keyword tags e.g. 'Gaia DR4', 'vsini', 'circumbinary'>"],
  "method_tags": ["<1-3 method tags e.g. 'forward model', 'TESS', 'eclipse timing'>"],
  "is_new_catalog": <true or false>,
  "cite_worthy": <true or false>,
  "new_result": "<2-4 word surprising result tag, or null>"
}}

Score: 10=circumbinary/Gaia vbroad, 8-9=stellar rotation/binaries, 6-7=related exoplanet science, 4-5=tangential, 1-3=not relevant
"""

        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}]
            )
            analysis = json.loads(response.content[0].text.strip())
            paper.update(analysis)
            analysed.append(paper)
        except Exception as e:
            print(f"    Error: {e}")
            paper.update({
                "relevance_score": paper["keyword_hits"],
                "plain_summary": paper["abstract"][:300] + "...",
                "why_interesting": "Matched your keywords.",
                "emoji": "📄",
                "highlight_phrase": paper["title"][:50],
                "kw_tags": [], "method_tags": [],
                "is_new_catalog": False, "cite_worthy": False, "new_result": None,
            })
            analysed.append(paper)

    result = [p for p in analysed if p.get("relevance_score", 0) >= CONFIG["min_score"]]
    result.sort(key=lambda p: p.get("relevance_score", 0), reverse=True)
    return result[:CONFIG["max_papers"]]


# ─────────────────────────────────────────────────────────────
#  HTML RENDERING
# ─────────────────────────────────────────────────────────────

def render_html(papers, date_str):

    def score_bar(score):
        filled = round(score)
        return "".join(["●" if i < filled else "○" for i in range(10)])

    def accent_color(score):
        if score >= 9: return "#4ade80"
        if score >= 8: return "#63b3ed"
        if score >= 7: return "#ecc94b"
        if score >= 6: return "#f6ad55"
        if score >= 5: return "#b794f4"
        return "#718096"

    def build_tags(p):
        score = p.get("relevance_score", 5)
        tags = []
        tags.append(f'<span class="tag tag-category">{p["category"]}</span>')
        tags.append(f'<span class="tag tag-date">{p["published"]}</span>')
        for a in p.get("known_authors", []):
            tags.append(f'<span class="tag tag-known">👋 {a}</span>')
        if score >= 9:
            tags.append('<span class="tag tag-hot">🔥 must-read</span>')
        if score >= 8:
            tags.append('<span class="tag tag-thesis">📌 thesis</span>')
        for kw in (p.get("kw_tags") or [])[:2]:
            tags.append(f'<span class="tag tag-gaia">{kw}</span>')
        if p.get("is_new_catalog"):
            tags.append('<span class="tag tag-catalog">📦 catalog</span>')
        if p.get("cite_worthy"):
            tags.append('<span class="tag tag-cite">📎 cite this</span>')
        if p.get("new_result"):
            tags.append(f'<span class="tag tag-new">{p["new_result"]}</span>')
        return "\n".join(tags)

    def build_method_tags(p):
        return "\n".join(f'<span class="tag tag-method">{t}</span>' for t in (p.get("method_tags") or []))

    # Highlights strip — top 2-4 papers
    highlights = [p for p in papers if p.get("relevance_score", 0) >= 7][:4]
    highlight_cards = ""
    for p in highlights:
        score = p.get("relevance_score", 5)
        hc = accent_color(score)
        htags = []
        if score >= 9: htags.append('<span class="hc-tag hot">🔥 must-read</span>')
        if score >= 8: htags.append('<span class="hc-tag thesis">📌 thesis</span>')
        if p.get("known_authors"): htags.append('<span class="hc-tag gaia">👋 known author</span>')
        for kw in (p.get("kw_tags") or [])[:2]:
            htags.append(f'<span class="hc-tag gaia">{kw}</span>')
        if p.get("is_new_catalog"): htags.append('<span class="hc-tag cat">📦 catalog</span>')
        if p.get("cite_worthy"): htags.append('<span class="hc-tag cite">📎 cite this</span>')

        highlight_cards += f"""
      <a class="highlight-card" href="{p['url']}" style="--hc:{hc}">
        <div class="hc-top">
          <div class="hc-tags">{''.join(htags[:3])}</div>
          <span class="hc-emoji">{p.get('emoji','🔭')}</span>
        </div>
        <div class="hc-headline">{p.get('highlight_phrase','')}</div>
        <div class="hc-blurb">{p.get('plain_summary','')[:120]}…</div>
        <div class="hc-score">
          <span class="hc-score-num">{score}</span>
          <span style="color:#4a5568;font-size:12px">/10</span>
          <span class="hc-score-bar">{score_bar(score)}</span>
        </div>
      </a>"""

    highlights_html = ""
    if highlight_cards:
        highlights_html = f"""
  <div class="highlights-section">
    <div class="highlights-label">✦ This edition's highlights</div>
    <div class="highlights-grid">{highlight_cards}</div>
  </div>"""

    # Paper cards
    cards_html = ""
    for i, p in enumerate(papers):
        score = p.get("relevance_score", 5)
        ac = accent_color(score)
        authors_display = ", ".join(p["authors"][:5])
        if len(p["authors"]) > 5:
            authors_display += f" +{len(p['authors'])-5} more"
        top_pick = ' top-pick' if i == 0 else ''
        top_label = '<div class="top-pick-label">⭑ Top pick</div>' if i == 0 else ''

        cards_html += f"""
    <div class="paper-card{top_pick}" style="--accent:{ac}">
      {top_label}
      <div class="card-header">
        <div class="card-meta">{build_tags(p)}</div>
        <div class="score-area">
          <span class="emoji-big">{p.get('emoji','🔭')}</span>
          <div>
            <span class="score-num">{score}<span class="score-denom">/10</span></span>
            <div class="score-bar">{score_bar(score)}</div>
          </div>
        </div>
      </div>
      <h2 class="highlight-phrase">{p.get('highlight_phrase','')}</h2>
      <h3 class="paper-title"><a href="{p['url']}">{p['title']}</a></h3>
      <p class="authors">{authors_display}</p>
      <div class="summary-block">
        <div class="summary-section">
          <div class="section-label">🧪 What they did</div>
          <p>{p.get('plain_summary','')}</p>
        </div>
        <div class="summary-section why-section">
          <div class="section-label">⭐ Why it matters to you</div>
          <p>{p.get('why_interesting','')}</p>
        </div>
      </div>
      <div class="card-footer">
        <div class="card-topic-tags">{build_method_tags(p)}</div>
        <a href="{p['url']}" class="read-btn">Read paper →</a>
      </div>
    </div>"""

    if not papers:
        cards_html = '<div class="no-papers">No highly relevant papers this period. The cosmos is quiet. ☕</div>'

    avg_score = round(sum(p.get("relevance_score",0) for p in papers) / max(len(papers),1), 1)
    known_count = sum(1 for p in papers if p.get("known_authors"))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Science News for Silke — {date_str}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,700;1,400&family=DM+Sans:wght@300;400;500&family=DM+Mono:wght@400&display=swap');
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0d0f14; color: #e2e8f0; font-family: 'DM Sans', sans-serif; font-weight: 300; line-height: 1.7; -webkit-font-smoothing: antialiased; }}
  .email-wrapper {{ max-width: 680px; margin: 0 auto; background: #0d0f14; }}
  .header {{ padding: 52px 44px 36px; background: linear-gradient(135deg, #0d0f14 0%, #141824 50%, #0d1520 100%); border-bottom: 1px solid rgba(255,255,255,0.06); position: relative; overflow: hidden; }}
  .header::before {{ content: ''; position: absolute; top: -80px; right: -80px; width: 380px; height: 380px; background: radial-gradient(circle, rgba(99,179,237,0.07) 0%, transparent 70%); pointer-events: none; }}
  .header::after {{ content: ''; position: absolute; bottom: -50px; left: 30px; width: 250px; height: 250px; background: radial-gradient(circle, rgba(236,201,75,0.05) 0%, transparent 70%); pointer-events: none; }}
  .star-field {{ position: absolute; top: 0; left: 0; right: 0; bottom: 0; pointer-events: none; overflow: hidden; }}
  .star {{ position: absolute; width: 1px; height: 1px; background: white; border-radius: 50%; animation: twinkle var(--dur, 3s) ease-in-out infinite; animation-delay: var(--delay, 0s); }}
  @keyframes twinkle {{ 0%, 100% {{ opacity: 0.15; }} 50% {{ opacity: 0.6; }} }}
  .header-eyebrow {{ font-family: 'DM Mono', monospace; font-size: 10px; letter-spacing: 0.3em; text-transform: uppercase; color: #63b3ed; margin-bottom: 14px; opacity: 0.75; position: relative; }}
  .header-title {{ font-family: 'Playfair Display', serif; font-size: 48px; font-weight: 700; line-height: 1.05; color: #f0f4ff; margin-bottom: 6px; position: relative; }}
  .header-title em {{ font-style: italic; color: #ecc94b; }}
  .header-tagline {{ font-size: 13px; color: #4a5568; font-style: italic; margin-top: 10px; position: relative; font-family: 'Playfair Display', serif; }}
  .header-stats {{ margin-top: 32px; display: flex; gap: 32px; flex-wrap: wrap; position: relative; }}
  .stat {{ display: flex; flex-direction: column; gap: 3px; }}
  .stat-num {{ font-family: 'Playfair Display', serif; font-size: 32px; color: #ecc94b; line-height: 1; }}
  .stat-label {{ font-size: 9px; text-transform: uppercase; letter-spacing: 0.18em; color: #4a5568; }}
  .header-subtitle {{ margin-top: 20px; font-family: 'DM Mono', monospace; font-size: 10px; color: #2d3748; letter-spacing: 0.08em; position: relative; border-top: 1px solid #1a202c; padding-top: 16px; }}
  .highlights-section {{ background: #0e111a; border-bottom: 1px solid #1a202c; padding: 28px 28px 24px; }}
  .highlights-label {{ font-family: 'DM Mono', monospace; font-size: 9px; letter-spacing: 0.28em; text-transform: uppercase; color: #ecc94b; margin-bottom: 18px; display: flex; align-items: center; gap: 10px; }}
  .highlights-label::after {{ content: ''; flex: 1; height: 1px; background: rgba(236,201,75,0.15); }}
  .highlights-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
  .highlight-card {{ background: #141824; border: 1px solid #1e2535; border-top: 2px solid var(--hc, #ecc94b); border-radius: 6px; padding: 16px 18px; text-decoration: none; color: inherit; display: block; }}
  .hc-top {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 10px; }}
  .hc-tags {{ display: flex; flex-wrap: wrap; gap: 5px; }}
  .hc-tag {{ font-family: 'DM Mono', monospace; font-size: 8.5px; letter-spacing: 0.08em; text-transform: uppercase; padding: 2px 7px; border-radius: 2px; background: rgba(255,255,255,0.04); color: #718096; border: 1px solid #2d3748; }}
  .hc-tag.hot {{ background: rgba(245,101,101,0.1); color: #fc8181; border-color: rgba(245,101,101,0.2); }}
  .hc-tag.gaia {{ background: rgba(99,179,237,0.1); color: #63b3ed; border-color: rgba(99,179,237,0.2); }}
  .hc-tag.thesis {{ background: rgba(236,201,75,0.1); color: #ecc94b; border-color: rgba(236,201,75,0.2); }}
  .hc-tag.cite {{ background: rgba(154,230,180,0.1); color: #68d391; border-color: rgba(154,230,180,0.2); }}
  .hc-tag.cat {{ background: rgba(183,148,244,0.1); color: #b794f4; border-color: rgba(183,148,244,0.2); }}
  .hc-emoji {{ font-size: 22px; line-height: 1; flex-shrink: 0; margin-left: 8px; }}
  .hc-headline {{ font-family: 'Playfair Display', serif; font-size: 14.5px; font-style: italic; color: #cbd5e0; line-height: 1.35; margin-bottom: 8px; }}
  .hc-blurb {{ font-size: 12px; color: #718096; line-height: 1.6; }}
  .hc-score {{ margin-top: 12px; display: flex; align-items: center; gap: 8px; }}
  .hc-score-num {{ font-family: 'Playfair Display', serif; font-size: 18px; color: var(--hc, #ecc94b); line-height: 1; }}
  .hc-score-bar {{ font-size: 7px; letter-spacing: 2px; color: var(--hc, #ecc94b); opacity: 0.5; font-family: monospace; }}
  .section-divider {{ padding: 20px 44px 14px; font-family: 'DM Mono', monospace; font-size: 9px; letter-spacing: 0.25em; text-transform: uppercase; color: #2d3748; display: flex; align-items: center; gap: 14px; }}
  .section-divider::after {{ content: ''; flex: 1; height: 1px; background: #1a202c; }}
  .papers-container {{ padding: 0 24px 52px; display: flex; flex-direction: column; gap: 18px; }}
  .paper-card {{ background: #141824; border: 1px solid #1e2535; border-left: 3px solid var(--accent, #63b3ed); border-radius: 8px; padding: 24px 26px 20px; position: relative; }}
  .paper-card.top-pick {{ background: linear-gradient(135deg, #141824 80%, #16201a 100%); }}
  .top-pick-label {{ position: absolute; top: -1px; right: 24px; font-family: 'DM Mono', monospace; font-size: 8.5px; letter-spacing: 0.2em; text-transform: uppercase; background: #4ade80; color: #0d1710; padding: 3px 10px; border-radius: 0 0 4px 4px; font-weight: 500; }}
  .card-header {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 12px; }}
  .card-meta {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; flex: 1; }}
  .tag {{ font-family: 'DM Mono', monospace; font-size: 9px; letter-spacing: 0.07em; text-transform: uppercase; padding: 2px 8px; border-radius: 2px; border: 1px solid transparent; white-space: nowrap; }}
  .tag-category {{ background: rgba(99,179,237,0.08); color: #63b3ed; border-color: rgba(99,179,237,0.18); }}
  .tag-date {{ color: #4a5568; padding-left: 0; border-color: transparent; background: none; }}
  .tag-known {{ background: rgba(236,201,75,0.08); color: #ecc94b; border-color: rgba(236,201,75,0.2); }}
  .tag-thesis {{ background: rgba(236,201,75,0.08); color: #ecc94b; border-color: rgba(236,201,75,0.2); }}
  .tag-hot {{ background: rgba(245,101,101,0.1); color: #fc8181; border-color: rgba(245,101,101,0.2); }}
  .tag-method {{ background: rgba(246,173,85,0.08); color: #c9895a; border-color: rgba(246,173,85,0.15); }}
  .tag-cite {{ background: rgba(154,230,180,0.1); color: #68d391; border-color: rgba(154,230,180,0.2); }}
  .tag-gaia {{ background: rgba(99,179,237,0.08); color: #90cdf4; border-color: rgba(99,179,237,0.18); }}
  .tag-catalog {{ background: rgba(183,148,244,0.1); color: #b794f4; border-color: rgba(183,148,244,0.2); }}
  .tag-new {{ background: rgba(154,230,180,0.08); color: #68d391; border-color: rgba(154,230,180,0.15); }}
  .score-area {{ display: flex; flex-direction: column; align-items: flex-end; gap: 4px; flex-shrink: 0; }}
  .emoji-big {{ font-size: 26px; line-height: 1; }}
  .score-num {{ font-family: 'Playfair Display', serif; font-size: 24px; color: var(--accent, #63b3ed); line-height: 1; }}
  .score-denom {{ font-size: 13px; color: #4a5568; }}
  .score-bar {{ font-size: 8px; letter-spacing: 2.5px; color: var(--accent, #63b3ed); opacity: 0.55; margin-top: 1px; font-family: monospace; }}
  .highlight-phrase {{ font-family: 'Playfair Display', serif; font-size: 15px; font-style: italic; color: #718096; font-weight: 400; margin-bottom: 7px; }}
  .paper-title {{ font-family: 'DM Sans', sans-serif; font-size: 16.5px; font-weight: 500; color: #e2e8f0; line-height: 1.4; margin-bottom: 6px; }}
  .paper-title a {{ color: inherit; text-decoration: none; }}
  .authors {{ font-family: 'DM Mono', monospace; font-size: 10.5px; color: #4a5568; margin-bottom: 18px; }}
  .summary-block {{ display: flex; flex-direction: column; gap: 10px; margin-bottom: 18px; }}
  .summary-section {{ background: rgba(255,255,255,0.02); border-radius: 5px; padding: 13px 15px; }}
  .why-section {{ background: rgba(236,201,75,0.03); border: 1px solid rgba(236,201,75,0.07); }}
  .section-label {{ font-family: 'DM Mono', monospace; font-size: 9px; letter-spacing: 0.2em; text-transform: uppercase; color: #4a5568; margin-bottom: 7px; }}
  .summary-section p {{ font-size: 13.5px; color: #94a3b8; line-height: 1.75; }}
  .why-section p {{ color: #b7a87a; }}
  .card-footer {{ display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; }}
  .card-topic-tags {{ display: flex; flex-wrap: wrap; gap: 5px; }}
  .read-btn {{ display: inline-block; font-family: 'DM Mono', monospace; font-size: 10px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--accent, #63b3ed); text-decoration: none; border: 1px solid var(--accent, #63b3ed); padding: 6px 14px; border-radius: 3px; opacity: 0.6; flex-shrink: 0; }}
  .no-papers {{ text-align: center; padding: 60px 24px; color: #4a5568; font-family: 'Playfair Display', serif; font-style: italic; font-size: 18px; }}
  .footer {{ padding: 36px 44px; border-top: 1px solid #141824; background: #0a0c11; }}
  .constellation {{ text-align: center; font-size: 18px; margin-bottom: 18px; opacity: 0.2; letter-spacing: 10px; }}
  .footer-text {{ font-family: 'DM Mono', monospace; font-size: 9.5px; color: #2d3748; letter-spacing: 0.1em; line-height: 2.2; text-align: center; }}
  .footer-text a {{ color: #4a5568; text-decoration: none; }}
</style>
</head>
<body>
<div class="email-wrapper">
  <div class="header">
    <div class="star-field">
      <div class="star" style="top:12%;left:8%;--dur:2.8s;--delay:0s;width:2px;height:2px"></div>
      <div class="star" style="top:25%;left:78%;--dur:3.5s;--delay:0.7s"></div>
      <div class="star" style="top:8%;left:55%;--dur:4s;--delay:0.3s"></div>
      <div class="star" style="top:45%;left:15%;--dur:3.1s;--delay:1.8s"></div>
      <div class="star" style="top:70%;left:42%;--dur:2.6s;--delay:0.9s"></div>
      <div class="star" style="top:18%;left:33%;--dur:3.8s;--delay:2.1s;width:2px;height:2px"></div>
      <div class="star" style="top:82%;left:67%;--dur:2.4s;--delay:0.5s"></div>
    </div>
    <div class="header-eyebrow">arXiv · astro-ph · {date_str}</div>
    <div class="header-title">Science News<br>for <em>Silke</em></div>
    <div class="header-tagline">Your bi-weekly window into the cosmos ✦ curated by Claude</div>
    <div class="header-stats">
      <div class="stat"><span class="stat-num">{len(papers)}</span><span class="stat-label">papers curated</span></div>
      <div class="stat"><span class="stat-num">{known_count}</span><span class="stat-label">familiar authors</span></div>
      <div class="stat"><span class="stat-num">{avg_score}</span><span class="stat-label">avg relevance</span></div>
    </div>
    <div class="header-subtitle">Monitoring astro-ph.EP · astro-ph.SR · astro-ph.GA · last {CONFIG['days_back']} days · threshold ≥ {CONFIG['min_score']}/10</div>
  </div>
  {highlights_html}
  <div class="section-divider">All papers this edition · {len(papers)} total</div>
  <div class="papers-container">{cards_html}</div>
  <div class="footer">
    <div class="constellation">✦ · ✦ · ✦ · ✦ · ✦</div>
    <div class="footer-text">
      Science News for Silke · Aarhus University · Department of Physics & Astronomy<br>
      Papers sourced from <a href="https://arxiv.org">arxiv.org</a> · Summaries generated by Claude · Running on GitHub Actions<br>
      <em>"Ad astra per aspera"</em>
    </div>
  </div>
</div>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────
#  EMAIL SENDING — Resend API
# ─────────────────────────────────────────────────────────────

def send_email(html, paper_count, date_str):
    # Always save HTML artifact — useful for debugging and as a backup
    with open("digest_output.html", "w") as f:
        f.write(html)
    print("💾 Saved digest_output.html")

    recipient = CONFIG["recipient_email"]
    api_key = os.environ.get("RESEND_API_KEY", "")

    if not all([recipient, api_key]):
        print("⚠️  RESEND_API_KEY or RECIPIENT_EMAIL not set — skipping email send.")
        return

    payload = json.dumps({
        "from": "Science News for Silke <onboarding@resend.dev>",
        "to": [recipient],
        "subject": f"🔭 Science News for Silke — {paper_count} papers · {date_str}",
        "html": html,
    }).encode()

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
            print(f"✅ Email sent to {recipient} (id: {result.get('id')})")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"❌ Resend API error {e.code}: {body}")
        print("📋 Digest was saved as digest_output.html artifact — check Actions artifacts to download it.")


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────

def main():
    date_str = datetime.utcnow().strftime("%B %d, %Y")
    print(f"\n🔭 Science News for Silke — {date_str}")
    print("=" * 50)

    print("\n📡 Fetching papers from arXiv...")
    papers = fetch_arxiv_papers(CONFIG["categories"], CONFIG["days_back"])

    print("\n🔍 Pre-filtering...")
    candidates = pre_filter(papers)
    print(f"   {len(candidates)} candidates")

    print("\n🤖 Analysing with Claude...")
    final_papers = analyse_papers(candidates)
    print(f"   {len(final_papers)} papers made the cut")

    print("\n🎨 Rendering HTML...")
    html = render_html(final_papers, date_str)

    print("\n📧 Sending email...")
    send_email(html, len(final_papers), date_str)

    print("\n✨ Done!\n")


if __name__ == "__main__":
    main()

