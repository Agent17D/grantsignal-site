#!/usr/bin/env python3
"""
GrantCommand Weekly Digest Pipeline
-----------------------------------
Fetches posted grants from Grants.gov, scores them for nonprofit/school
relevance, builds FREE (top 3) and PAID (top 50) digest emails, sends them
via Resend to segmented Beehiiv subscribers, and publishes an archive post
on Beehiiv.

Run: python digest.py
Env vars required (or set as GitHub Actions secrets):
  BEEHIIV_API_KEY, BEEHIIV_PUB_ID, RESEND_API_KEY, FROM_EMAIL

Optional:
  GITHUB_TOKEN  — required to load subscriber preferences from data/preferences/
  GITHUB_REPO   — defaults to Agent17D/grantcommand-site
  DRY_RUN       — set to "true" to preview personalization without sending emails
"""

import os
import sys
import json
import math
import random
import hashlib
import datetime
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BEEHIIV_API_KEY = os.environ.get("BEEHIIV_API_KEY", "")
BEEHIIV_PUB_ID  = os.environ.get("BEEHIIV_PUB_ID",  "")
RESEND_API_KEY  = os.environ.get("RESEND_API_KEY",  "")
FROM_EMAIL      = os.environ.get("FROM_EMAIL", "digest@grantcommand.com")
GITHUB_TOKEN    = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO     = os.environ.get("GITHUB_REPO", "Agent17D/grantcommand-site")
GITHUB_BRANCH   = os.environ.get("GITHUB_BRANCH", "main")
DRY_RUN         = os.environ.get("DRY_RUN", "false").lower() == "true"

GRANTS_GOV_ENDPOINT = "https://api.grants.gov/v1/api/search2"

MAX_PAID_GRANTS = 50
MIN_SCORE = 2.5
URGENCY_DAYS    = 14

NAVY  = "#0f3460"
BLUE  = "#1565c0"
TEAL  = "#00897b"

_ARCHIVE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "archive")

# ---------------------------------------------------------------------------
# Exclusion / eligibility filters
# ---------------------------------------------------------------------------

EXCLUDE_TITLE_PATTERNS = [
    "R25", "R01", "R03", "R21", "R34", "K08", "K23", "F31", "F32", "T32",
    "Clinical Trial", "Dissertation", "Fellowship", "Postdoctoral",
    "Career Development Award",
    "EONS 2018", "Appendix E", "MUREP",
    "NAGPRA", "Repatriation Grants", "Subaward",
    "Information Collection", "Comment Request",
    "FY 2012", "FY 2013", "FY 2014", "FY 2015", "FY 2016",
    "FY 2017", "FY 2018", "FY 2019", "FY 2020",
    "Developing Methodologies", "Coastal Impacts", "Climate Variability",
]

RESEARCH_AGENCY_PATTERNS = [
    "NIH", "National Cancer Institute", "National Heart",
    "National Institute of", "AHRQ", "National Science Foundation",
    "NSF", "National Endowment for the Humanities",
]

COMMUNITY_RELEVANCE_KEYWORDS = [
    "community", "nonprofit", "school", "education",
    "prevention", "outreach", "public health",
]

ELIGIBLE_CODES = {
    "nonprofits", "private", "public", "independent",
    "special", "small", "unrestricted", "other",
}

NONPROFIT_KEYWORDS = [
    "nonprofit", "non-profit", "501(c)(3)", "501c3",
    "community organization", "community-based",
    "school", "school district", "education", "educational institution",
    "university", "college", "higher education",
    "faith-based", "faith based", "public library", "library system",
]

# ---------------------------------------------------------------------------
# Scoring categories
# ---------------------------------------------------------------------------

SCORING_CATEGORIES = {
    "education":             ["education", "school", "learning", "literacy", "stem",
                              "tutoring", "after-school", "after school", "student",
                              "teacher", "curriculum", "early childhood"],
    "health":                ["health", "mental health", "substance abuse", "opioid",
                              "nutrition", "wellness", "public health", "clinic",
                              "behavioral health", "maternal", "infant", "senior health"],
    "arts":                  ["arts", "culture", "music", "theater", "theatre",
                              "humanities", "creative", "heritage", "museum", "library"],
    "environment":           ["environment", "climate", "conservation", "sustainability",
                              "clean energy", "renewable", "watershed", "wildlife",
                              "green infrastructure", "resilience"],
    "community_development": ["community development", "economic development",
                              "workforce", "job training", "small business",
                              "revitalization", "neighborhood", "rural development",
                              "urban development", "capacity building"],
    "youth":                 ["youth", "children", "juvenile", "teen", "adolescent",
                              "child welfare", "foster", "mentoring", "mentorship"],
    "housing":               ["housing", "affordable housing", "homelessness", "shelter",
                              "transitional housing", "rental assistance", "homeownership"],
    "social_services":       ["social service", "food bank", "food security", "hunger",
                              "disability", "veteran", "refugee", "immigrant",
                              "domestic violence", "human trafficking", "poverty",
                              "low-income", "low income", "underserved"],
}

GRANT_WRITING_TIPS = [
    "Always read the full NOFO before starting your application. Eligibility requirements are often buried in Section C.",
    "Federal grants require a DUNS/UEI number and SAM.gov registration. Make sure yours is active — it can take 2-3 weeks to process.",
    "Deadline means RECEIVED by deadline, not postmarked. Submit at least 48 hours early to avoid technical issues.",
    "Match requirements are common in federal grants — know your organization's in-kind and cash match capacity before applying.",
    "Start with smaller grants ($25K-$100K) to build a track record. Federal agencies favor organizations with prior federal award experience.",
]

# ---------------------------------------------------------------------------
# Budget ceiling map for premium filtering
# ---------------------------------------------------------------------------

BUDGET_CEILING_MAP = {
    "Under $100K":   200_000,
    "$100K – $500K": 750_000,
    "$500K – $1M":   2_000_000,
    "$1M – $5M":     None,
    "Over $5M":      None,
    "Any size":      None,
}


# ---------------------------------------------------------------------------
# Step 0 — Load subscriber preferences
# ---------------------------------------------------------------------------

def email_hash(email: str) -> str:
    """Return SHA-256 hex digest of lowercase email (mirrors the Worker)."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def load_subscriber_preferences() -> dict:
    """
    Fetch all files from data/preferences/ in the GitHub repo.

    Returns dict keyed by email hash:
      { "abc123…": { email_hash, email, state, budget, grant_size, … } }

    Returns {} if GITHUB_TOKEN is unset or on any error.
    """
    if not GITHUB_TOKEN:
        print("[preferences] GITHUB_TOKEN not set — skipping preference load.")
        return {}

    print("[preferences] Loading subscriber preferences from GitHub…")
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/data/preferences"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept":        "application/vnd.github.v3+json",
        "User-Agent":    "GrantCommand-Digest/1.0",
    }

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 404:
            print("[preferences] data/preferences/ not found — no preferences yet.")
            return {}
        resp.raise_for_status()
        files = resp.json()
    except requests.RequestException as exc:
        print(f"[preferences] ERROR listing directory: {exc}")
        return {}

    if not isinstance(files, list):
        print("[preferences] Unexpected GitHub API response.")
        return {}

    json_files = [f for f in files if f.get("name", "").endswith(".json")]
    print(f"[preferences] Found {len(json_files)} preference file(s).")

    preferences = {}
    for fe in json_files:
        dl_url = fe.get("download_url")
        if not dl_url:
            continue
        try:
            r = requests.get(dl_url, headers=headers, timeout=15)
            r.raise_for_status()
            prefs = r.json()
            h = prefs.get("email_hash") or fe["name"].replace(".json", "")
            preferences[h] = prefs
        except (requests.RequestException, ValueError, KeyError) as exc:
            print(f"[preferences] WARN skipping {fe.get('name')}: {exc}")

    print(f"[preferences] Loaded {len(preferences)} preference record(s).")
    return preferences


# ---------------------------------------------------------------------------
# Premium personalization
# ---------------------------------------------------------------------------

def filter_for_premium(grants: list, prefs: dict) -> tuple:
    """
    Apply preference-based filtering to a grant list for a premium subscriber.

    Args:
        grants: Full scored grant list.
        prefs:  Subscriber's preferences dict.

    Returns:
        (filtered_grants, notice_lines)
        notice_lines is a list of strings to show at the top of the digest.
    """
    notice_lines = []
    filtered     = list(grants)

    # ── Geography note ───────────────────────────────────────────────────────
    state = (prefs.get("state") or "").strip()
    if state and state.lower() not in ("nationwide / multiple states", "nationwide", ""):
        notice_lines.append(
            f"📍 Geography: You operate in {state}. "
            "Showing all grants — geographic filtering coming soon as grant data improves."
        )

    # ── Budget filter ────────────────────────────────────────────────────────
    budget      = prefs.get("budget") or "Any size"
    max_ceiling = BUDGET_CEILING_MAP.get(budget)

    if max_ceiling is not None:
        before   = len(filtered)
        filtered = [g for g in filtered if _passes_budget_filter(g, max_ceiling)]
        removed  = before - len(filtered)
        notice_lines.append(
            f"💰 Budget filter ({budget}): {removed} grant(s) removed "
            f"(award ceiling > ${max_ceiling:,})."
        )

    return filtered, notice_lines


def _passes_budget_filter(grant: dict, max_ceiling: int) -> bool:
    award = grant.get("awardCeiling") or grant.get("award_ceiling")
    if award is None:
        return True  # No ceiling data → include
    try:
        val = float(str(award).replace(",", "").replace("$", ""))
        return val <= max_ceiling
    except (ValueError, TypeError):
        return True


# ---------------------------------------------------------------------------
# Step 1 — Fetch grants from Grants.gov
# ---------------------------------------------------------------------------

def fetch_grants(max_records: int = 500) -> list:
    print(f"[grants.gov] Fetching posted grants (max {max_records})…")
    all_hits  = []
    page_size = 100
    offset    = 0

    while len(all_hits) < max_records:
        payload = {
            "oppStatuses": "posted",
            "rows":        page_size,
            "startRecord": offset,
            "fields": (
                "id,oppNumber,title,agencyName,openDate,closeDate,"
                "fundingCategory,eligibilities,synopsis,awardCeiling"
            ),
        }
        try:
            resp = requests.post(GRANTS_GOV_ENDPOINT, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            print(f"[grants.gov] ERROR at offset={offset}: {exc}")
            break
        except json.JSONDecodeError as exc:
            print(f"[grants.gov] JSON decode error at offset={offset}: {exc}")
            break

        hits = data.get("data", {}).get("oppHits", [])
        if not hits:
            break

        all_hits.extend(hits)
        print(f"[grants.gov] {len(all_hits)} grants retrieved…")

        total = data.get("data", {}).get("hitCount", 0)
        if len(all_hits) >= total:
            break
        offset += page_size

    print(f"[grants.gov] Total: {len(all_hits)} grants.")
    return all_hits[:max_records]


# ---------------------------------------------------------------------------
# Step 2 — Filter for nonprofit / school eligibility
# ---------------------------------------------------------------------------

def _is_research_agency(agency: str) -> bool:
    au = agency.upper()
    return any(p.upper() in au for p in RESEARCH_AGENCY_PATTERNS)


def _has_community_relevance(title: str, synopsis: str) -> bool:
    text = (title + " " + synopsis).lower()
    return any(kw.lower() in text for kw in COMMUNITY_RELEVANCE_KEYWORDS)


def is_eligible(grant: dict) -> bool:
    import re
    title    = grant.get("title",    "") or ""
    synopsis = grant.get("synopsis", "") or ""
    agency   = grant.get("agency",   "") or ""

    for pattern in EXCLUDE_TITLE_PATTERNS:
        if len(pattern) <= 3:
            if re.search(r'\b' + re.escape(pattern) + r'\b', title, re.IGNORECASE):
                return False
        else:
            if pattern.lower() in title.lower():
                return False

    if _is_research_agency(agency) and not _has_community_relevance(title, synopsis):
        return False

    for elig in (grant.get("eligibilities", []) or []):
        label = (elig.get("label", "") or "").lower()
        code  = (elig.get("code",  "") or "").lower()
        if any(kw in label or kw in code for kw in ELIGIBLE_CODES):
            return True
        if "unrestricted" in label or "unrestricted" in code:
            return True

    return True  # Survived exclusion filters → include


# ---------------------------------------------------------------------------
# Step 3 — Score grants
# ---------------------------------------------------------------------------

def score_grant(grant: dict) -> float:
    title    = grant.get("title",        "") or ""
    synopsis = grant.get("synopsis",     "") or ""
    agency   = grant.get("agency",       "") or ""
    close_dt = grant.get("closeDate",    "") or ""
    award    = grant.get("awardCeiling", None)
    text     = (title + " " + synopsis + " " + agency).lower()

    score = 2.0
    matched = sum(1 for kws in SCORING_CATEGORIES.values() if any(kw in text for kw in kws))
    score += (matched / len(SCORING_CATEGORIES)) * 3.0

    if "nonprofit" in text or "community organization" in text:
        score += 1.0

    try:
        if award is not None and float(str(award).replace(",", "")) < 500_000:
            score += 0.5
    except (ValueError, TypeError):
        pass

    if close_dt:
        try:
            close_date = datetime.datetime.strptime(close_dt, "%m/%d/%Y").date()
            days_left  = (close_date - datetime.date.today()).days
            if 0 <= days_left <= 60:
                score += 1.0
        except ValueError:
            pass

    return round(min(5.0, score), 1)


# ---------------------------------------------------------------------------
# Step 4 — Build digest lists
# ---------------------------------------------------------------------------

def build_digests(grants: list) -> tuple:
    eligible = [g for g in grants if is_eligible(g)]
    print(f"[digest] {len(eligible)} grants passed eligibility filter.")

    for g in eligible:
        g["_score"] = score_grant(g)

    eligible.sort(key=lambda g: g["_score"], reverse=True)
    eligible = [g for g in eligible if g["_score"] >= MIN_SCORE]
    print(f"[digest] {len(eligible)} grants meet score threshold ({MIN_SCORE}).")

    return eligible[:3], eligible[:MAX_PAID_GRANTS]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def stars_html(score: float) -> tuple:
    filled = '<span style="color:#f4a800;">&#9733;</span>'
    empty  = '<span style="color:#cccccc;">&#9734;</span>'
    if   score >= 4.5: return filled*5,         "Excellent Match", TEAL
    elif score >= 3.5: return filled*4 + empty,  "Strong Match",   BLUE
    elif score >= 2.5: return filled*3 + empty*2,"Good Match",     "#5a6a7a"
    else:              return filled*2 + empty*3, "Possible Match", "#9e9e9e"


def is_urgent(close_date_str: str) -> bool:
    if not close_date_str:
        return False
    try:
        d = datetime.datetime.strptime(close_date_str, "%m/%d/%Y").date()
        return 0 <= (d - datetime.date.today()).days <= URGENCY_DAYS
    except ValueError:
        return False


def urgency_flag(close_date_str: str) -> str:
    if not close_date_str:
        return ""
    try:
        d         = datetime.datetime.strptime(close_date_str, "%m/%d/%Y").date()
        days_left = (d - datetime.date.today()).days
        if 0 <= days_left <= URGENCY_DAYS:
            return f"⚡ Closes in {days_left}d"
    except ValueError:
        pass
    return ""


def grants_gov_url(opp_number: str) -> str:
    return f"https://www.grants.gov/search-results-detail/{opp_number}"


def _format_close(raw: str) -> str:
    import re as _re
    if _re.match(r"\d{2}/\d{2}/\d{4}", raw):
        try:
            return datetime.datetime.strptime(raw, "%m/%d/%Y").strftime("%b %-d, %Y")
        except ValueError:
            pass
    return raw or "See Grants.gov for deadline"


def _grant_card_html(grant: dict) -> str:
    score        = grant.get("_score", 0)
    title        = grant.get("title",      "Untitled") or "Untitled"
    agency       = grant.get("agency", "Unknown Agency") or "Unknown Agency"
    raw_close    = grant.get("closeDate",  "") or ""
    opp_num      = grant.get("number",     "") or ""
    synopsis_raw = grant.get("synopsis",   "") or ""
    synopsis     = "" if (not synopsis_raw or synopsis_raw == title) else synopsis_raw[:120]
    urgent       = is_urgent(raw_close)
    close_fmt    = _format_close(raw_close)
    url          = grants_gov_url(opp_num) if opp_num else "https://www.grants.gov"
    stars_str, match_label, match_color = stars_html(score)
    border_color = "#e53935" if urgent else "#00897b"

    close_html = (
        f'<span style="color:#e53935;font-weight:bold;">&#9889; Closes {close_fmt} &mdash; URGENT</span>'
        if urgent else
        f'&#128197; Closes {close_fmt}'
    )
    synopsis_html = (
        f'<div style="font-size:13px;color:#5a6a7a;line-height:1.6;margin-bottom:14px;">'
        f'{synopsis}{"..." if len(synopsis_raw) > 120 else ""}</div>'
        if synopsis else ""
    )

    return f"""
        <div style="background:#ffffff;border-radius:10px;
                    box-shadow:0 2px 8px rgba(0,0,0,0.08);
                    margin:0 0 16px 0;padding:20px 24px;
                    border-left:4px solid {border_color};">
          <div style="font-size:15px;font-weight:bold;color:#0f3460;
                      margin-bottom:6px;line-height:1.4;">{title}</div>
          <div style="font-size:12px;color:#1565c0;margin-bottom:8px;">&#127963; {agency}</div>
          <div style="margin-bottom:8px;font-size:14px;">
            {stars_str}
            <span style="font-size:12px;color:{match_color};margin-left:6px;font-weight:600;">{match_label}</span>
          </div>
          <div style="font-size:13px;color:#444;margin-bottom:8px;">{close_html}</div>
          {synopsis_html}
          <div>
            <a href="{url}" style="display:inline-block;background:#00897b;color:#ffffff;
                                   font-size:13px;font-weight:bold;padding:8px 18px;
                                   border-radius:6px;text-decoration:none;">
              View on Grants.gov &rarr;
            </a>
          </div>
        </div>"""


# ---------------------------------------------------------------------------
# Step 5a — Build FREE HTML email
# ---------------------------------------------------------------------------

def build_free_html(grants: list, total_matched: int, urgency_count: int = 0) -> str:
    week_str   = datetime.date.today().strftime("%B %d, %Y")
    grant_html = "".join(_grant_card_html(g) for g in grants)

    upgrade_cta = f"""
    <div style="background:#0f3460;border-radius:10px;padding:24px 28px;margin:8px 0 0 0;">
      <div style="color:#ffffff;font-size:15px;font-weight:600;margin-bottom:8px;">
        You're seeing 3 of {total_matched} grants matched this week
      </div>
      <div style="color:#90a8c0;font-size:13px;line-height:1.5;margin-bottom:18px;">
        Upgrade to Basic to see all {total_matched} opportunities &mdash;
        including {urgency_count} closing soon
      </div>
      <a href="https://grantcommand.beehiiv.com/upgrade"
         style="display:inline-block;background:#00897b;color:#ffffff;
                font-size:15px;font-weight:bold;padding:12px 28px;
                border-radius:8px;text-decoration:none;">
        Upgrade for $29/month &rarr;
      </a>
    </div>"""

    return _email_wrapper(
        title=f"&#128225; GrantCommand | Your Top 3 Federal Grant Matches This Week",
        preheader="Your top 3 federal grant matches this week — curated for nonprofits and schools.",
        header_sub="Federal Grant Intelligence",
        week_str=week_str,
        intro="Good morning &mdash; here are your top 3 federal grant matches this week, selected for nonprofits and schools like yours.",
        body=grant_html + upgrade_cta,
        footer_sub="You're receiving this because you subscribed to GrantCommand's free tier.",
        show_archive_link=False,
    )


# ---------------------------------------------------------------------------
# Step 5b — Build PAID HTML email (with optional personalization notices)
# ---------------------------------------------------------------------------

def build_paid_html(grants: list, notice_lines: list = None) -> str:
    week_str   = datetime.date.today().strftime("%B %d, %Y")
    count      = len(grants)
    tip        = random.choice(GRANT_WRITING_TIPS)
    grant_html = "".join(_grant_card_html(g) for g in grants)

    notice_html = ""
    if notice_lines:
        items = "".join(
            f'<div style="font-size:13px;color:#1565c0;margin-bottom:6px;">{line}</div>'
            for line in notice_lines
        )
        notice_html = f"""
    <div style="background:#e3f2fd;border-radius:10px;padding:16px 20px;margin:0 0 16px 0;
                border-left:4px solid #1565c0;">
      <div style="font-size:11px;font-weight:700;color:#0f3460;margin-bottom:8px;
                  text-transform:uppercase;letter-spacing:1px;">&#10024; Your Personalized Digest</div>
      {items}
    </div>"""

    tip_section = f"""
    <div style="background:#e3f2fd;border-radius:10px;padding:20px 24px;margin:8px 0 0 0;">
      <div style="font-size:15px;font-weight:bold;color:#0f3460;margin-bottom:10px;">&#128161; This Week's Tip</div>
      <div style="font-size:14px;color:#1565c0;line-height:1.6;">{tip}</div>
    </div>"""

    return _email_wrapper(
        title=f"&#128225; GrantCommand | Full Weekly Digest &mdash; {count} Grants Matched",
        preheader=f"Your full weekly digest — {count} federal grant matches curated for nonprofits and schools.",
        header_sub=f"Full Weekly Digest &mdash; {count} Grants Matched",
        week_str=week_str,
        intro=f"Your full weekly digest of federal grant matches, sorted by relevance score. {count} opportunities matched this week.",
        body=notice_html + grant_html + tip_section,
        footer_sub="Full digest &mdash; Basic/Premium subscriber",
        show_archive_link=True,
    )


def _email_wrapper(title, preheader, header_sub, week_str, intro, body,
                   footer_sub, show_archive_link=False) -> str:
    archive_link = (
        '&nbsp;&middot;&nbsp;<a href="https://grantcommand.com/archive" '
        'style="color:#00897b;text-decoration:underline;">Archive</a>'
        if show_archive_link else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title}</title>
</head>
<body style="margin:0;padding:0;background:#f4f7fb;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#222222;">
  <div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">
    {preheader}
    &nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;
  </div>
  <div style="max-width:600px;margin:0 auto;padding:24px 16px;">
    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#0f3460;border-radius:10px 10px 0 0;">
      <tr>
        <td style="padding:22px 28px;">
          <div style="color:#ffffff;font-size:22px;font-weight:bold;line-height:1.2;">&#128225; GrantCommand</div>
          <div style="color:#4fc3f7;font-size:12px;margin-top:4px;">{header_sub}</div>
        </td>
        <td align="right" style="padding:22px 28px;vertical-align:top;">
          <div style="color:#90a8c0;font-size:12px;white-space:nowrap;">{week_str}</div>
        </td>
      </tr>
    </table>
    <div style="background:#ffffff;padding:18px 28px;border-bottom:1px solid #e8eef4;">
      <p style="margin:0;font-size:14px;color:#5a6a7a;line-height:1.6;">{intro}</p>
    </div>
    <div style="background:#f4f7fb;padding:20px 16px;">
      {body}
    </div>
    <div style="background:#e8eef4;padding:16px 28px;border-radius:0 0 10px 10px;text-align:center;">
      <div style="font-size:13px;color:#5a6a7a;font-weight:600;">&#128225; GrantCommand &middot; grantcommand.com</div>
      <div style="font-size:12px;color:#8a9ab0;margin-top:6px;line-height:1.5;">{footer_sub}</div>
      <div style="font-size:12px;margin-top:10px;">
        <a href="{{{{unsubscribe_url}}}}" style="color:#00897b;text-decoration:underline;">Unsubscribe</a>
        {archive_link}
      </div>
    </div>
  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Step 6 — Fetch Beehiiv subscribers
# ---------------------------------------------------------------------------

def fetch_subscribers(tier: str) -> list:
    print(f"[beehiiv] Fetching {tier} subscribers…")
    emails = []
    page   = 1

    while True:
        url     = f"https://api.beehiiv.com/v2/publications/{BEEHIIV_PUB_ID}/subscriptions"
        params  = {"status": "active", "tier": tier, "page": page, "limit": 100}
        headers = {"Authorization": f"Bearer {BEEHIIV_API_KEY}", "Content-Type": "application/json"}

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            print(f"[beehiiv] ERROR (page {page}): {exc}")
            break

        subs = data.get("data", [])
        if not subs:
            break

        emails.extend(sub["email"] for sub in subs if sub.get("email"))

        if page >= data.get("total_pages", 1):
            break
        page += 1

    print(f"[beehiiv] Found {len(emails)} {tier} subscribers.")
    return emails


# ---------------------------------------------------------------------------
# Step 7 — Send emails via Resend
# ---------------------------------------------------------------------------

def send_email_batch(to_emails: list, subject: str, html_body: str, label: str = "batch") -> None:
    if not to_emails:
        print(f"[resend] No recipients for {label}.")
        return

    print(f"[resend] Sending {label} to {len(to_emails)} subscribers…")
    success = errors = 0

    for i, email in enumerate(to_emails, start=1):
        try:
            resp = requests.post(
                "https://api.resend.com/emails",
                json={"from": FROM_EMAIL, "to": [email], "subject": subject, "html": html_body},
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                timeout=20,
            )
            resp.raise_for_status()
            success += 1
        except requests.RequestException as exc:
            errors += 1
            print(f"[resend] WARN {email}: {exc}")

        if i % 10 == 0:
            print(f"[resend] {i}/{len(to_emails)} sent…")

    print(f"[resend] {label} done — {success} sent, {errors} errors.")


def _send_one(email: str, subject: str, html_body: str) -> bool:
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            json={"from": FROM_EMAIL, "to": [email], "subject": subject, "html": html_body},
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            timeout=20,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f"[resend] WARN {email}: {exc}")
        return False


# ---------------------------------------------------------------------------
# Step 8 — Archive
# ---------------------------------------------------------------------------

def _render_grant_card(grant: dict, rank: int) -> str:
    score    = grant.get("_score", 0)
    title    = grant.get("title",      "Untitled") or "Untitled"
    agency   = grant.get("agency", "Unknown Agency") or "Unknown Agency"
    close_dt = grant.get("closeDate",  "") or ""
    opp_num  = grant.get("number",     "") or ""
    synopsis = (grant.get("synopsis",  "") or "")[:500]
    urgency  = urgency_flag(close_dt)
    url      = grants_gov_url(opp_num) if opp_num else "https://www.grants.gov"

    full = int(score)
    half = 1 if (score - full) >= 0.5 else 0
    stars_display = "⭐" * full + ("✨" if half else "") + "☆" * (5 - full - half)

    def esc(s):
        return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")

    css = "grant-card" + (" high-fit" if score >= 4.0 else "") + (" urgent" if urgency else "")
    urgency_html = f'<span class="grant-urgency">⚡ {urgency}</span>' if urgency else ""

    return f"""
    <div class="{css}">
      <div class="grant-rank">#{rank}</div>
      <div class="grant-agency">{esc(agency)}</div>
      <h3 class="grant-title"><a href="{esc(url)}" target="_blank" rel="noopener">{esc(title)}</a></h3>
      <div class="grant-scores">
        <span class="grant-stars">{stars_display}</span>
        <span class="grant-fit-label">Fit Score: {score}/5</span>
        {urgency_html}
      </div>
      <p class="grant-synopsis">{esc(synopsis)}{"…" if len(synopsis)==500 else ""}</p>
      <div class="grant-footer">
        <span class="grant-close-date">Closes: <strong>{esc(close_dt) or "See Grants.gov"}</strong></span>
        <a href="{esc(url)}" target="_blank" rel="noopener" class="grant-link">View on Grants.gov →</a>
      </div>
    </div>"""


def save_archive_entry(grants: list, week_date: datetime.date) -> list:
    os.makedirs(_ARCHIVE_DIR, exist_ok=True)

    slug        = week_date.strftime("%Y-%m-%d")
    week_label  = week_date.strftime("Week of %b %-d, %Y")
    issue_date  = week_date.strftime("%B %-d, %Y")
    grant_count = len(grants)
    top_grants  = [g.get("title","") for g in grants[:5] if g.get("title")]

    cards_html = (
        "\n".join(_render_grant_card(g, i+1) for i, g in enumerate(grants))
        if grants
        else '<p style="text-align:center;padding:32px;">No grants matched this week.</p>'
    )

    template_path = os.path.join(_ARCHIVE_DIR, "digest-template.html")
    page_html = (
        open(template_path, encoding="utf-8").read()
        if os.path.exists(template_path)
        else "<html><body>{{GRANT_CARDS}}</body></html>"
    )

    for k, v in {
        "{{PAGE_TITLE}}":       f"GrantCommand — {week_label}",
        "{{OG_TITLE}}":         f"GrantCommand Digest — {week_label}",
        "{{OG_DESCRIPTION}}":   f"{grant_count} federal grant opportunities matched this week.",
        "{{META_DESCRIPTION}}": f"{grant_count} federal grant opportunities matched this week.",
        "{{SLUG}}":             slug,
        "{{WEEK_LABEL}}":       week_label,
        "{{DIGEST_TITLE}}":     week_label,
        "{{ISSUE_DATE}}":       issue_date,
        "{{GRANT_COUNT}}":      str(grant_count),
        "{{GRANT_CARDS}}":      cards_html,
    }.items():
        page_html = page_html.replace(k, v)

    html_path = os.path.join(_ARCHIVE_DIR, f"{slug}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(page_html)
    print(f"[archive] Written {html_path}")

    issues_path = os.path.join(_ARCHIVE_DIR, "issues.json")
    issues = []
    if os.path.exists(issues_path):
        try:
            issues = json.load(open(issues_path, encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    issues = [i for i in issues if i.get("slug") != slug]
    issues.insert(0, {"date": slug, "title": week_label, "slug": slug,
                       "grant_count": grant_count, "top_grants": top_grants})

    with open(issues_path, "w", encoding="utf-8") as f:
        json.dump(issues, f, indent=2, ensure_ascii=False)
    print(f"[archive] Updated issues.json ({len(issues)} issues)")

    return [f"archive/{slug}.html", "archive/issues.json"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 60)
    print("  GrantCommand Weekly Digest Pipeline")
    print(f"  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    if DRY_RUN:
        print("  ⚠️  DRY RUN MODE — emails will NOT be sent")
    print("=" * 60)

    missing = [v for v in ("BEEHIIV_API_KEY","BEEHIIV_PUB_ID","RESEND_API_KEY","FROM_EMAIL")
               if not os.environ.get(v)]
    if missing:
        print(f"ERROR: Missing env vars: {', '.join(missing)}")
        sys.exit(1)

    # ── 0. Load subscriber preferences ──────────────────────────────────────
    all_prefs = load_subscriber_preferences()

    # ── 1. Fetch & deduplicate grants ────────────────────────────────────────
    grants = fetch_grants(max_records=500)
    if not grants:
        print("No grants fetched. Exiting.")
        sys.exit(1)

    seen, unique = set(), []
    for g in grants:
        gid = str(g.get("id") or g.get("number") or g.get("title",""))
        if gid not in seen:
            seen.add(gid)
            unique.append(g)
    print(f"[dedup] {len(grants)} → {len(unique)} unique grants")
    grants = unique

    # Filter out expired grants (close date in the past)
    today = datetime.date.today()
    active = []
    for g in grants:
        raw = g.get("closeDate","") or ""
        if raw:
            try:
                import re as _re
                if _re.match(r"\d{2}/\d{2}/\d{4}", raw):
                    close_dt = datetime.datetime.strptime(raw, "%m/%d/%Y").date()
                    if close_dt < today:
                        continue  # expired — skip
            except ValueError:
                pass
        active.append(g)
    removed = len(grants) - len(active)
    if removed:
        print(f"[expiry] Removed {removed} expired grants ({len(active)} remaining)")
    grants = active

    # ── 2–4. Filter, score, build digests ───────────────────────────────────
    free_digest, paid_digest = build_digests(grants)
    total_matched = len(paid_digest)

    if not paid_digest:
        print("No eligible grants. Exiting.")
        sys.exit(0)

    urgency_count = sum(1 for g in paid_digest if is_urgent(g.get("closeDate","") or ""))
    print(f"[digest] Free: {len(free_digest)} | Paid: {total_matched} | Urgent: {urgency_count}")

    # ── 5. Build default email HTML ──────────────────────────────────────────
    free_html         = build_free_html(free_digest, total_matched, urgency_count)
    default_paid_html = build_paid_html(paid_digest)

    # ── 6. Fetch subscribers ─────────────────────────────────────────────────
    free_subs = fetch_subscribers("free")
    paid_subs = fetch_subscribers("premium")

    week_str = datetime.date.today().strftime("%B %d, %Y")

    # ── 7. Send ──────────────────────────────────────────────────────────────
    if DRY_RUN:
        print(f"\n[DRY RUN] FREE: would send to {len(free_subs)} subscribers.")
        print(f"[DRY RUN] PREMIUM: would send to {len(paid_subs)} subscribers.")
        if paid_subs and all_prefs:
            print("\n[DRY RUN] Personalization preview (first 5 premium subs):")
            for sub_email in paid_subs[:5]:
                h     = email_hash(sub_email)
                prefs = all_prefs.get(h)
                if prefs:
                    filtered, notices = filter_for_premium(paid_digest, prefs)
                    print(f"  {sub_email[:25]}… → {len(filtered)} grants "
                          f"(was {total_matched}), notices: {notices}")
                else:
                    print(f"  {sub_email[:25]}… → no preferences, default digest")
    else:
        send_email_batch(free_subs,
                         "📡 GrantCommand | Your Top 3 Federal Grant Matches This Week",
                         free_html, "FREE")

        print(f"[digest] Sending personalized digests to {len(paid_subs)} premium subscribers…")
        ok = err = 0
        for sub_email in paid_subs:
            h     = email_hash(sub_email)
            prefs = all_prefs.get(h)

            if prefs:
                filtered, notices = filter_for_premium(paid_digest, prefs)
                sub_html  = build_paid_html(filtered, notice_lines=notices)
                count_lbl = len(filtered)
            else:
                sub_html  = default_paid_html
                count_lbl = total_matched

            subject = f"📡 GrantCommand | {count_lbl} Federal Grant Matches This Week"
            if _send_one(sub_email, subject, sub_html):
                ok += 1
            else:
                err += 1

        print(f"[resend] PREMIUM done — {ok} sent, {err} errors.")

    # ── 8. Save archive ──────────────────────────────────────────────────────
    changed = save_archive_entry(paid_digest, datetime.date.today())
    if changed:
        print(f"[archive] Files written: {', '.join(changed)}")

    print("=" * 60)
    print("  Pipeline complete ✅")
    print("=" * 60)


if __name__ == "__main__":
    main()
