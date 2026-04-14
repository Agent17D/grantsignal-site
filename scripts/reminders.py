#!/usr/bin/env python3
"""
GrantSignal Thursday Deadline Reminder
----------------------------------------
Fetches the most recent digest from archive/issues.json, parses grant close
dates from the archived HTML, and sends a reminder email to Premium subscribers
for any grants closing within 7 days.

Run: python scripts/reminders.py
Env vars required (same as digest.py):
  BEEHIIV_API_KEY, BEEHIIV_PUB_ID, RESEND_API_KEY, FROM_EMAIL

Optional:
  GITHUB_TOKEN  — for fetching archive files from private repo (if needed)
  DRY_RUN       — set to "true" to print output without sending emails
  ARCHIVE_BASE_URL — URL base for fetching archive HTML
                     defaults to https://grantsignal.news/archive
"""

import os
import re
import sys
import json
import datetime
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BEEHIIV_API_KEY  = os.environ.get("BEEHIIV_API_KEY", "")
BEEHIIV_PUB_ID   = os.environ.get("BEEHIIV_PUB_ID",  "")
RESEND_API_KEY   = os.environ.get("RESEND_API_KEY",  "")
FROM_EMAIL       = os.environ.get("FROM_EMAIL", "digest@grantsignal.news")
GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO      = os.environ.get("GITHUB_REPO", "Agent17D/grantcommand-site")
DRY_RUN          = os.environ.get("DRY_RUN", "false").lower() == "true"

GITHUB_API_BASE  = "https://api.github.com"
URGENCY_DAYS     = 7   # grants closing within this many days
SUPPORT_EMAIL    = "support@grantsignal.news"
SITE_URL         = "https://grantsignal.news"

NAVY  = "#0f3460"
BLUE  = "#1565c0"
TEAL  = "#00897b"


# ---------------------------------------------------------------------------
# Step 1: Fetch most recent issue from archive/issues.json via GitHub API
# ---------------------------------------------------------------------------

def fetch_issues_json() -> list:
    """
    Fetch archive/issues.json from GitHub repo.
    Returns list of issue dicts (newest first).
    """
    headers = _github_headers()
    url = f"{GITHUB_API_BASE}/repos/{GITHUB_REPO}/contents/archive/issues.json"

    print("[archive] Fetching issues.json from GitHub…")
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()

    import base64
    content = base64.b64decode(resp.json()["content"]).decode("utf-8")
    issues = json.loads(content)
    print(f"[archive] Found {len(issues)} issues.")
    return issues


def fetch_digest_html(slug: str) -> str:
    """
    Fetch archive/{slug}.html from GitHub repo.
    Returns the raw HTML string.
    """
    headers = _github_headers()
    url = f"{GITHUB_API_BASE}/repos/{GITHUB_REPO}/contents/archive/{slug}.html"

    print(f"[archive] Fetching archive/{slug}.html…")
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()

    import base64
    content = base64.b64decode(resp.json()["content"]).decode("utf-8")
    print(f"[archive] Fetched {len(content)} bytes of HTML.")
    return content


def _github_headers() -> dict:
    h = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "GrantSignal-Reminders/1.0",
    }
    if GITHUB_TOKEN:
        h["Authorization"] = f"token {GITHUB_TOKEN}"
    return h


# ---------------------------------------------------------------------------
# Step 2: Parse urgent grants from digest HTML
# ---------------------------------------------------------------------------

def parse_urgent_grants(html: str) -> list:
    """
    Scan digest HTML for grants closing within URGENCY_DAYS days.

    Looks for patterns like:
      - "Closes Jan 15, 2026"
      - "Closes in Nd" (urgency flag already embedded)
      - grant title links

    Returns list of dicts: { title, url, close_date_str, days_left }
    """
    today = datetime.date.today()
    urgent = []

    # Match grant card blocks — look for title + close date pairs
    # Pattern: find <h3 class="grant-title"> or similar, then nearby close date

    # Strategy: find all "Closes: YYYY-MM-DD" or "Closes: Mon DD, YYYY" strings
    # and their neighboring grant titles / URLs from the card structure

    # Pattern 1: grant-footer close date
    # <span class="grant-close-date">Closes: <strong>MM/DD/YYYY</strong></span>
    close_pattern = re.compile(
        r'<span[^>]*grant-close-date[^>]*>Closes:\s*<strong>([^<]+)</strong>',
        re.IGNORECASE
    )
    # Pattern 2: grant title with link
    title_pattern = re.compile(
        r'<a[^>]+href="([^"]+)"[^>]*>([^<]{10,200})</a>',
        re.IGNORECASE
    )

    # Split by grant-card divs and process each one
    card_pattern = re.compile(
        r'<div[^>]*class="[^"]*grant-card[^"]*"[^>]*>(.*?)</div>\s*</div>',
        re.DOTALL | re.IGNORECASE
    )

    # Fallback: just find all close dates and check urgency
    all_close_dates = close_pattern.findall(html)
    all_titles      = title_pattern.findall(html)

    # Try to pair them by position
    close_matches = list(re.finditer(
        r'<span[^>]*grant-close-date[^>]*>Closes:\s*<strong>([^<]+)</strong>',
        html, re.IGNORECASE
    ))
    title_matches = list(re.finditer(
        r'class="grant-title"[^>]*>.*?<a\s+href="([^"]+)"[^>]*>([^<]{10,200})</a>',
        html, re.DOTALL | re.IGNORECASE
    ))

    used_titles = set()

    for cm in close_matches:
        date_str = cm.group(1).strip()
        pos      = cm.start()

        close_date = _parse_date(date_str)
        if close_date is None:
            continue
        days_left = (close_date - today).days
        if not (0 <= days_left <= URGENCY_DAYS):
            continue

        # Find the nearest title before this position
        best_title = None
        best_url   = None
        best_dist  = float('inf')

        for tm in title_matches:
            if tm.end() <= pos and (pos - tm.end()) < best_dist:
                tid = tm.group(1) + tm.group(2)
                if tid not in used_titles:
                    best_dist  = pos - tm.end()
                    best_title = tm.group(2).strip()
                    best_url   = tm.group(1).strip()

        if best_title and best_url:
            tid = best_url + best_title
            if tid not in used_titles:
                used_titles.add(tid)
                urgent.append({
                    "title":          best_title,
                    "url":            best_url,
                    "close_date_str": date_str,
                    "close_date":     close_date,
                    "days_left":      days_left,
                })

    # Deduplicate by URL
    seen_urls = set()
    deduped = []
    for g in urgent:
        if g["url"] not in seen_urls:
            seen_urls.add(g["url"])
            deduped.append(g)

    deduped.sort(key=lambda g: g["days_left"])
    return deduped


def _parse_date(date_str: str):
    """Parse a date string into a datetime.date. Returns None on failure."""
    date_str = date_str.strip()
    formats = [
        "%m/%d/%Y",         # 01/15/2026
        "%b %-d, %Y",       # Jan 15, 2026
        "%b %d, %Y",        # Jan 05, 2026
        "%B %-d, %Y",       # January 15, 2026
        "%B %d, %Y",        # January 05, 2026
        "%Y-%m-%d",         # 2026-01-15
    ]
    for fmt in formats:
        try:
            return datetime.datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Step 3: Build reminder email HTML
# ---------------------------------------------------------------------------

def build_reminder_html(urgent_grants: list, week_label: str) -> str:
    """Build a clean HTML reminder email for the urgent grants."""
    today_str = datetime.date.today().strftime("%B %d, %Y")
    n         = len(urgent_grants)

    grant_rows = ""
    for g in urgent_grants:
        days = g["days_left"]
        urgency_color = "#e53935" if days <= 3 else "#e65100" if days <= 5 else "#f57c00"
        grant_rows += f"""
    <div style="background:#ffffff;border-radius:8px;
                box-shadow:0 2px 8px rgba(0,0,0,0.08);
                margin:0 0 14px 0;padding:18px 22px;
                border-left:4px solid {urgency_color};">
      <div style="font-size:14px;font-weight:bold;color:#0f3460;
                  margin-bottom:6px;line-height:1.4;">
        {_escape(g['title'])}
      </div>
      <div style="font-size:13px;font-weight:700;color:{urgency_color};margin-bottom:10px;">
        ⚡ Closes {_escape(g['close_date_str'])} — {days} day{'s' if days != 1 else ''} left
      </div>
      <a href="{_escape(g['url'])}"
         style="display:inline-block;background:#00897b;color:#ffffff;
                font-size:13px;font-weight:bold;padding:8px 16px;
                border-radius:6px;text-decoration:none;">
        View on Grants.gov →
      </a>
    </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>⚡ GrantSignal | {n} Grant{'s' if n != 1 else ''} Closing This Week</title>
</head>
<body style="margin:0;padding:0;background:#f4f7fb;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             color:#222222;">
  <!-- Preheader -->
  <div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">
    ⚡ {n} grant{'s' if n != 1 else ''} from this week's digest {'are' if n != 1 else 'is'} closing within 7 days.
    &nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;
  </div>

  <div style="max-width:600px;margin:0 auto;padding:24px 16px;">

    <!-- Header -->
    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#0f3460;border-radius:10px 10px 0 0;">
      <tr>
        <td style="padding:22px 28px;">
          <div style="color:#ffffff;font-size:22px;font-weight:bold;">⚡ GrantSignal</div>
          <div style="color:#4fc3f7;font-size:12px;margin-top:4px;">Thursday Deadline Alert</div>
        </td>
        <td align="right" style="padding:22px 28px;vertical-align:top;">
          <div style="color:#90a8c0;font-size:12px;white-space:nowrap;">{today_str}</div>
        </td>
      </tr>
    </table>

    <!-- Intro -->
    <div style="background:#ffffff;padding:18px 28px;border-bottom:1px solid #e8eef4;">
      <p style="margin:0;font-size:14px;color:#5a6a7a;line-height:1.6;">
        <strong style="color:#0f3460;">{n} grant{'s' if n != 1 else ''} from {week_label}</strong>
        {'are' if n != 1 else 'is'} closing within the next 7 days.
        Don't let these slip by — review and apply before the deadline.
      </p>
    </div>

    <!-- Grant rows -->
    <div style="background:#f4f7fb;padding:20px 16px;">
      {grant_rows}
    </div>

    <!-- Footer -->
    <div style="background:#e8eef4;padding:16px 28px;border-radius:0 0 10px 10px;text-align:center;">
      <div style="font-size:13px;color:#5a6a7a;font-weight:600;">
        ⚡ GrantSignal Thursday Alert &middot; grantsignal.news
      </div>
      <div style="font-size:12px;color:#8a9ab0;margin-top:6px;line-height:1.5;">
        You're receiving this as a GrantSignal Premium subscriber.<br>
        Alerts only send when grants are closing within 7 days.
      </div>
      <div style="font-size:12px;margin-top:10px;">
        <a href="{{{{unsubscribe_url}}}}" style="color:#00897b;text-decoration:underline;">Unsubscribe</a>
        &nbsp;&middot;&nbsp;
        <a href="https://grantsignal.news/archive" style="color:#00897b;text-decoration:underline;">View Archive</a>
        &nbsp;&middot;&nbsp;
        <a href="mailto:{SUPPORT_EMAIL}" style="color:#00897b;text-decoration:underline;">Support</a>
      </div>
    </div>

  </div>
</body>
</html>"""


def _escape(s: str) -> str:
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


# ---------------------------------------------------------------------------
# Step 4: Fetch Premium subscribers from Beehiiv
# ---------------------------------------------------------------------------

def fetch_premium_subscribers() -> list:
    """Fetch all active premium subscriber emails from Beehiiv."""
    print("[beehiiv] Fetching premium subscribers…")
    emails = []
    page   = 1

    while True:
        url = (f"https://api.beehiiv.com/v2/publications/{BEEHIIV_PUB_ID}"
               f"/subscriptions")
        params = {"status": "active", "tier": "premium", "page": page, "limit": 100}
        headers = {
            "Authorization": f"Bearer {BEEHIIV_API_KEY}",
            "Content-Type":  "application/json",
        }

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

        for sub in subs:
            email = sub.get("email")
            if email:
                emails.append(email)

        if page >= data.get("total_pages", 1):
            break
        page += 1

    print(f"[beehiiv] Found {len(emails)} premium subscribers.")
    return emails


# ---------------------------------------------------------------------------
# Step 5: Send reminder emails via Resend
# ---------------------------------------------------------------------------

def send_reminder_batch(emails: list, subject: str, html_body: str) -> None:
    """Send reminder emails to all premium subscribers."""
    if not emails:
        print("[resend] No recipients — skipping send.")
        return

    print(f"[resend] Sending reminder to {len(emails)} subscribers…")
    success = 0
    errors  = 0

    for i, email in enumerate(emails, start=1):
        payload = {
            "from":    FROM_EMAIL,
            "to":      [email],
            "subject": subject,
            "html":    html_body,
        }
        try:
            resp = requests.post(
                "https://api.resend.com/emails",
                json=payload,
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type":  "application/json",
                },
                timeout=20,
            )
            resp.raise_for_status()
            success += 1
        except requests.RequestException as exc:
            errors += 1
            print(f"[resend] WARN failed to send to {email}: {exc}")

        if i % 10 == 0:
            print(f"[resend] Progress: {i}/{len(emails)} sent…")

    print(f"[resend] Done — {success} sent, {errors} errors.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 60)
    print("  GrantSignal Thursday Deadline Reminder")
    print(f"  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    if DRY_RUN:
        print("  ⚠️  DRY RUN MODE — emails will NOT be sent")
    print("=" * 60)

    # Validate env
    missing = [v for v in ("BEEHIIV_API_KEY", "BEEHIIV_PUB_ID",
                            "RESEND_API_KEY", "FROM_EMAIL")
               if not os.environ.get(v)]
    if missing:
        print(f"ERROR: Missing env vars: {', '.join(missing)}")
        sys.exit(1)

    if not GITHUB_TOKEN:
        print("ERROR: GITHUB_TOKEN is required to fetch archive files.")
        sys.exit(1)

    # ── 1. Get most recent issue ─────────────────────────────────────────────
    try:
        issues = fetch_issues_json()
    except Exception as exc:
        print(f"ERROR fetching issues.json: {exc}")
        sys.exit(1)

    if not issues:
        print("No issues found in archive/issues.json. Exiting.")
        sys.exit(0)

    latest = issues[0]
    slug       = latest.get("slug", "")
    week_label = latest.get("title", slug)
    print(f"[main] Latest issue: {week_label} (slug: {slug})")

    if not slug:
        print("ERROR: Latest issue has no slug. Exiting.")
        sys.exit(1)

    # ── 2. Fetch digest HTML ─────────────────────────────────────────────────
    try:
        html = fetch_digest_html(slug)
    except Exception as exc:
        print(f"ERROR fetching archive/{slug}.html: {exc}")
        sys.exit(1)

    # ── 3. Parse urgent grants ───────────────────────────────────────────────
    urgent = parse_urgent_grants(html)

    if not urgent:
        print(f"[main] No urgent grants found this week — skipping reminder. ✅")
        sys.exit(0)

    print(f"[main] Found {len(urgent)} urgent grants:")
    for g in urgent:
        print(f"  • {g['title'][:70]}… — closes {g['close_date_str']} ({g['days_left']}d)")

    # ── 4. Build email ───────────────────────────────────────────────────────
    n       = len(urgent)
    subject = f"⚡ GrantSignal | {n} Grant{'s' if n != 1 else ''} Closing This Week"
    html_body = build_reminder_html(urgent, week_label)

    if DRY_RUN:
        print(f"\n[DRY RUN] Subject: {subject}")
        print(f"[DRY RUN] Would send to premium subscribers (not fetched in dry run).")
        print("[DRY RUN] Email HTML preview (first 500 chars):")
        print(html_body[:500])
        sys.exit(0)

    # ── 5. Fetch premium subscribers ─────────────────────────────────────────
    emails = fetch_premium_subscribers()
    if not emails:
        print("[main] No premium subscribers found. Exiting.")
        sys.exit(0)

    # ── 6. Send ──────────────────────────────────────────────────────────────
    send_reminder_batch(emails, subject, html_body)

    print("=" * 60)
    print("  Thursday reminder complete ✅")
    print("=" * 60)


if __name__ == "__main__":
    main()
