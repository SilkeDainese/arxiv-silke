"""
Profile Scraper — extracts keywords and co-authors for the arXiv Digest setup wizard.

Search: uses the ORCID public API (free, no authentication required) to find researchers
by name. Returns name, ORCID URL, and department (affiliation).

Primary extract path: fetch_orcid_works() — hits the ORCID /works API to get publication
titles and derive keywords. No authentication, no Cloudflare issues, works for any
ORCID-registered researcher.

Fallback extract path: scrape_pure_profile() — scrapes Pure portal HTML (AU Pure and
compatible instances). Requires a browser-like User-Agent. Many researchers do not have
a Pure page, and pure.au.dk search is Cloudflare-blocked, so Pure is relegated to
optional manual fallback.

All functions are fault-tolerant and return empty results on failure rather than raising.
"""

import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────────────────────

# Browser-like User-Agent required by Cloudflare-protected Pure portals.
# The generic library UA ("arxiv-digest-setup/1.0") triggers 403 on pure.au.dk.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_BROWSER_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_ORCID_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "arxiv-digest-setup/1.0 (https://github.com/SilkeDainese/arxiv-digest)",
}

# Words to exclude from keyword extraction
STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "been",
    "be", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "shall", "can", "need", "must",
    "that", "which", "who", "whom", "this", "these", "those", "it", "its",
    "their", "our", "your", "my", "we", "they", "he", "she", "i", "me",
    "him", "her", "us", "them", "not", "no", "nor", "so", "if", "then",
    "than", "too", "very", "just", "about", "above", "after", "again",
    "all", "also", "any", "because", "before", "between", "both", "each",
    "few", "more", "most", "other", "over", "same", "some", "such",
    "through", "under", "until", "up", "what", "when", "where", "while",
    "how", "here", "there", "into", "during", "only", "own", "new",
    "using", "based", "via", "non", "two", "three", "first", "one",
    "well", "however", "high", "low", "large", "small", "study",
    "results", "show", "find", "found", "use", "used", "model", "data",
    "analysis", "method", "methods", "effect", "effects", "properties",
    "measurements", "observations", "paper", "work", "present",
}


# ─────────────────────────────────────────────────────────────
#  Profile Search (ORCID)
# ─────────────────────────────────────────────────────────────

def fetch_orcid_person(orcid_id: str) -> tuple[str, str, str | None]:
    """
    Fetch name and primary institution for a known ORCID ID.

    Returns (full_name, institution, error). institution is empty string if no
    employment record exists. error is None on success or a message string on failure.
    """
    try:
        person_resp = requests.get(
            f"https://pub.orcid.org/v3.0/{orcid_id}/person",
            headers=_ORCID_HEADERS,
            timeout=10,
        )
        person_resp.raise_for_status()
        person = person_resp.json()
        name_info = person.get("name", {}) or {}
        given = (name_info.get("given-names") or {}).get("value", "")
        family = (name_info.get("family-name") or {}).get("value", "")
        full_name = f"{given} {family}".strip()
        if not full_name:
            return "", "", "No name found on this ORCID profile."
    except Exception as e:
        return "", "", str(e)

    institution = ""
    try:
        emp_resp = requests.get(
            f"https://pub.orcid.org/v3.0/{orcid_id}/employments",
            headers=_ORCID_HEADERS,
            timeout=10,
        )
        if emp_resp.status_code == 200:
            aff_groups = emp_resp.json().get("affiliation-group", [])
            if aff_groups:
                summaries = aff_groups[0].get("summaries", [])
                if summaries:
                    emp = summaries[0].get("employment-summary", {})
                    institution = (emp.get("organization") or {}).get("name", "")
    except Exception:
        pass  # Institution is optional — don't fail the whole fetch

    return full_name, institution, None


def search_pure_profiles(name: str, institution: str = "", base_url: str = "https://pure.au.dk") -> list[dict]:
    """
    Search for researcher profiles by name using the ORCID public API.

    The Pure portal search endpoint (pure.au.dk/portal/en/searchAll.html) is
    protected by Cloudflare and returns 403 to automated requests. The ORCID
    public API is free, requires no authentication, and is the reliable alternative.

    The `base_url` parameter is accepted for interface compatibility but is not
    used — ORCID search is institution-agnostic.

    Args:
        name: researcher name to search for (e.g. "Silke Dainese")
        base_url: ignored (kept for backward-compatible interface)

    Returns:
        list of dicts with keys: name, url, department.
        url points to the ORCID profile page.
        Returns empty list on any failure.
    """
    if not name or not name.strip():
        return []

    # ── Parse given/family names for a more precise ORCID query ──
    # Use first word as given name and last word as family name so that middle
    # names (e.g. "Silke Sofia Dainese") don't pollute the family-name field.
    parts = name.strip().split()
    if len(parts) >= 2:
        given = parts[0]
        family = parts[-1]
        query = f"given-names:{given} AND family-name:{family}"
    else:
        query = name.strip()

    if institution and institution.strip():
        query += f' AND affiliation-org-name:"{institution.strip()}"'

    def _orcid_search(q: str) -> list:
        try:
            resp = requests.get(
                "https://pub.orcid.org/v3.0/search",
                params={"q": q, "rows": 10},
                headers=_ORCID_HEADERS,
                timeout=15,
            )
            resp.raise_for_status()
            return [
                r.get("orcid-identifier", {}).get("path", "")
                for r in resp.json().get("result", [])
                if r.get("orcid-identifier", {}).get("path")
            ]
        except Exception:
            return []

    orcid_ids = _orcid_search(query)
    # Broader fallback: drop institution constraint first, then try family name only
    if not orcid_ids and institution and len(parts) >= 2:
        orcid_ids = _orcid_search(f"given-names:{given} AND family-name:{family}")
    if not orcid_ids and len(parts) >= 2:
        orcid_ids = _orcid_search(f"family-name:{family}")

    results = []
    for orcid_id in orcid_ids[:10]:
        try:
            person_resp = requests.get(
                f"https://pub.orcid.org/v3.0/{orcid_id}/person",
                headers=_ORCID_HEADERS,
                timeout=10,
            )
            if person_resp.status_code != 200:
                continue
            person = person_resp.json()

            name_info = person.get("name", {}) or {}
            given_val = (name_info.get("given-names") or {}).get("value", "")
            family_val = (name_info.get("family-name") or {}).get("value", "")
            full_name = f"{given_val} {family_val}".strip()
            if not full_name:
                continue

            # Affiliation from employment summary
            affiliations_resp = requests.get(
                f"https://pub.orcid.org/v3.0/{orcid_id}/employments",
                headers=_ORCID_HEADERS,
                timeout=10,
            )
            dept = ""
            if affiliations_resp.status_code == 200:
                aff_groups = affiliations_resp.json().get("affiliation-group", [])
                if aff_groups:
                    summaries = aff_groups[0].get("summaries", [])
                    if summaries:
                        emp = summaries[0].get("employment-summary", {})
                        org = emp.get("organization", {}) or {}
                        dept = org.get("name", "")

            results.append({
                "name": full_name,
                "url": f"https://orcid.org/{orcid_id}",
                "department": dept,
            })
        except Exception:
            continue

    return results


# ─────────────────────────────────────────────────────────────
#  Profile Scrape (Pure portal HTML)
# ─────────────────────────────────────────────────────────────

def scrape_pure_profile(url: str) -> tuple[dict | None, list | None, str | None]:
    """
    Scrape a Pure profile page to extract publication keywords and co-authors.

    Works with AU Pure (pure.au.dk) and most compatible Pure instances.
    Uses a browser-like User-Agent to pass Cloudflare protection on the profile page.

    Note: this function scrapes the Pure HTML portal, not ORCID. The user must
    supply a direct Pure profile URL (e.g. from the search results or pasted manually).
    ORCID profile URLs (orcid.org) are not scraped here.

    Args:
        url: Pure profile URL

    Returns:
        (keywords_dict, coauthors_list, error) where keywords_dict maps keyword to
        weight (1-10), coauthors_list is a list of author name strings, and error is
        None on success or an error message string on failure.
        Returns (None, None, error_str) on failure.
    """
    try:
        resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        return None, None, str(e)

    soup = BeautifulSoup(resp.text, "html.parser")

    # ── Extract publication titles ──
    titles = []
    for selector in [
        "h3.title a",
        ".result-container h3 a",
        ".portal_list_item h2 a",
        ".rendering_contributiontojournal h3 a",
        ".rendering h3 a",
        "h2.dc_title a",
        ".list-results h3 a",
    ]:
        found = soup.select(selector)
        if found:
            titles = [el.get_text(strip=True) for el in found]
            break

    # Fallback: any h2/h3 inside result containers
    if not titles:
        for container_class in ["result-container", "portal_list_item", "list-results"]:
            containers = soup.find_all(class_=container_class)
            if containers:
                for c in containers:
                    h = c.find(["h2", "h3"])
                    if h:
                        titles.append(h.get_text(strip=True))
                break

    if not titles:
        return None, None, (
            "Could not find publication titles on this page. "
            "The page structure may differ from expected Pure formats, or the page "
            "may be Cloudflare-protected and not returning full content."
        )

    # ── Extract keywords from titles ──
    word_counts: Counter[str] = Counter()
    bigram_counts: Counter[str] = Counter()

    for title in titles:
        words = re.findall(r"[a-zA-Z][a-zA-Z-]{2,}", title.lower())
        words = [w for w in words if w not in STOPWORDS and len(w) > 2]
        word_counts.update(words)
        for i in range(len(words) - 1):
            bigram_counts.update([f"{words[i]} {words[i+1]}"])

    # Prefer bigrams that appear 2+ times, then single words
    combined: Counter[str] = Counter()
    for bigram, count in bigram_counts.items():
        if count >= 2:
            combined[bigram] = count * 2
    for word, count in word_counts.items():
        if count >= 2 and not any(word in bg for bg in combined):
            combined[word] = count

    if not combined:
        combined = word_counts

    keywords: dict[str, int] = {}
    if combined:
        max_count = max(combined.values())
        for term, count in combined.most_common(20):
            keywords[term] = max(1, round(10 * count / max_count))

    # ── Extract co-authors ──
    coauthors: set[str] = set()
    NAV_WORDS = {
        "home", "search", "contact", "about", "publications", "projects",
        "activities", "research", "profile", "overview", "back", "next",
        "previous", "more", "show all", "view all", "see all", "login",
        "log in", "sign in", "menu", "navigate", "skip", "department",
    }
    for selector in [
        "a[rel='Person']",
        ".person-list a",
        ".result-container .persons a",
        ".portal_list_item .authors a",
        ".rendering span.person a",
    ]:
        found = soup.select(selector)
        if found:
            for el in found:
                name = el.get_text(strip=True)
                if not name or len(name) < 4:
                    continue
                if " " not in name and "," not in name:
                    continue
                if name.lower() in NAV_WORDS:
                    continue
                coauthors.add(name)
            break

    # Remove the profile owner from the co-author list
    owner_name = None
    for selector in ["h1", ".profile-name", ".person-name", "h2.name"]:
        el = soup.select_one(selector)
        if el:
            owner_name = el.get_text(strip=True)
            break
    if owner_name:
        coauthors.discard(owner_name)

    return keywords, sorted(coauthors), None


# ─────────────────────────────────────────────────────────────
#  Publication Fetch (ORCID works API)
# ─────────────────────────────────────────────────────────────

def fetch_orcid_works(
    orcid_id: str,
) -> tuple[
    dict | None,
    list[str] | None,
    list[dict] | None,
    dict | None,
    dict | None,
    str | None,
]:
    """
    Fetch publications from the ORCID public API, derive keywords, and collect co-authors.

    Hits the /works summary endpoint. Co-authors are extracted from the contributor
    fields present in each work summary; ORCID does not guarantee these are complete,
    but repeat appearances across many papers reliably identify close collaborators.

    Args:
        orcid_id: bare ORCID identifier (e.g. "0000-0001-7568-6674")

    Returns:
        (keywords_dict, titles, works_meta, coauthor_orcids_dict, coauthor_counts, error)
        - keywords_dict maps keyword to weight (1-10)
        - titles is the list of raw publication titles (for AI summary)
        - works_meta is a list of {"title": str, "year": int | None} dicts, one per
          publication, in ORCID order (most-recent first for most profiles)
        - coauthor_orcids_dict maps ORCID ID -> display name for co-authors who have ORCIDs;
          a plain name list for those without ORCIDs is folded into the dict with "" keys
        - coauthor_counts maps display name -> number of shared-paper appearances
        - error is None on success or an error string on failure
        Returns (None, None, None, None, None, error_str) on failure.

    Note on author-position data: the ORCID /works summary endpoint does not reliably
    return contributor sequences. works_meta therefore omits author position — callers
    should fall back to year-descending order for smart pre-selection.
    """
    try:
        resp = requests.get(
            f"https://pub.orcid.org/v3.0/{orcid_id}/works",
            headers=_ORCID_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as e:
        return None, None, None, None, None, str(e)

    titles: list[str] = []
    works_meta: list[dict] = []
    # orcid_id -> name for co-authors with ORCIDs; name -> "" for those without
    coauthor_map: dict[str, str] = {}
    coauthor_counts: Counter[str] = Counter()
    name_appearances: Counter[str] = Counter()

    for group in resp.json().get("group", []):
        for summary in group.get("work-summary", []):
            title_obj = (summary.get("title") or {}).get("title") or {}
            title_value = title_obj.get("value", "").strip()
            if title_value:
                titles.append(title_value)
                # Extract publication year — present in most ORCID summary records
                pub_date = summary.get("publication-date") or {}
                year_obj = pub_date.get("year") or {}
                year_str = year_obj.get("value", "") if isinstance(year_obj, dict) else ""
                try:
                    year: int | None = int(year_str) if year_str else None
                except (ValueError, TypeError):
                    year = None
                works_meta.append({"title": title_value, "year": year})

            # Collect contributors from this work summary
            contributors = (summary.get("contributors") or {}).get("contributor", [])
            for contrib in contributors:
                role = ((contrib.get("contributor-attributes") or {}).get("contributor-role") or "").lower()
                if role and role not in ("author", ""):
                    continue
                name = ((contrib.get("credit-name") or {}).get("value") or "").strip()
                if not name or len(name) < 3:
                    continue
                contrib_orcid = ((contrib.get("contributor-orcid") or {}).get("path") or "").strip()
                if contrib_orcid and contrib_orcid != orcid_id:
                    coauthor_map[contrib_orcid] = name
                    coauthor_counts[name] += 1
                elif name:
                    name_appearances[name] += 1
                    coauthor_counts[name] += 1
            break  # First summary per group is sufficient for titles

    if not titles:
        return None, None, None, None, None, "No publications found on this ORCID profile."

    # Add name-only co-authors who appeared in 2+ papers (reduces noise)
    for name, count in name_appearances.items():
        if count >= 2:
            coauthor_map[f"__name__{name}"] = name

    # Keyword extraction — bigram-then-unigram logic
    word_counts: Counter[str] = Counter()
    bigram_counts: Counter[str] = Counter()

    for title in titles:
        words = re.findall(r"[a-zA-Z][a-zA-Z-]{2,}", title.lower())
        words = [w for w in words if w not in STOPWORDS and len(w) > 2]
        word_counts.update(words)
        for i in range(len(words) - 1):
            bigram_counts.update([f"{words[i]} {words[i+1]}"])

    combined: Counter[str] = Counter()
    for bigram, count in bigram_counts.items():
        if count >= 2:
            combined[bigram] = count * 2
    for word, count in word_counts.items():
        if count >= 2 and not any(word in bg for bg in combined):
            combined[word] = count

    if not combined:
        combined = word_counts

    keywords: dict[str, int] = {}
    if combined:
        max_count = max(combined.values())
        for term, count in combined.most_common(20):
            keywords[term] = max(1, round(10 * count / max_count))

    return keywords, titles, works_meta, coauthor_map, dict(coauthor_counts), None


def find_au_colleagues(
    coauthor_map: dict[str, str],
    coauthor_counts: dict[str, int] | None = None,
    institution: str = "Aarhus University",
    max_checks: int = 50,
) -> list[str]:
    """
    Filter co-authors to those affiliated with the given institution.

    Checks ORCID employment records in parallel for co-authors who have ORCID IDs.
    Co-authors without ORCIDs (keyed as '__name__<name>') are excluded — no way to
    verify affiliation without an ORCID.

    Args:
        coauthor_map: dict returned by fetch_orcid_works (orcid_id -> name, or __name__name -> name)
        coauthor_counts: optional display-name frequency map from fetch_orcid_works
        institution: institution name to match (case-insensitive substring)
        max_checks: max number of ORCID lookups to perform

    Returns:
        List of display names confirmed to be at the given institution.
    """
    candidates = {
        orcid_id: name
        for orcid_id, name in coauthor_map.items()
        if not orcid_id.startswith("__name__")
    }
    if not candidates:
        return []

    inst_lower = institution.lower()
    au_colleagues: list[str] = []
    counts = coauthor_counts or {}

    ranked_candidates = sorted(
        candidates.items(),
        key=lambda item: (-counts.get(item[1], 0), item[1].lower()),
    )[:max_checks]

    def _check(orcid_id: str, name: str) -> str | None:
        _, emp_institution, error = fetch_orcid_person(orcid_id)
        if not error and inst_lower in emp_institution.lower():
            return name
        return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_check, oid, nm): nm
            for oid, nm in ranked_candidates
        }
        for future in as_completed(futures):
            result = future.result()
            if result:
                au_colleagues.append(result)

    return au_colleagues
