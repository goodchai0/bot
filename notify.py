#!/usr/bin/env python3
"""
notify.py - turns new_jobs.csv into a location-grouped email (and optional Telegram message).

Usage:
    python3 notify.py                          # email everything, grouped by city
    python3 notify.py --cities Bengaluru,Remote  # only these locations
    python3 notify.py --dry-run                # print summary only: no email, no archive

Settings come from a .env file in the same folder (see .env.example), or environment variables:
    EMAIL_USER, EMAIL_APP_PASSWORD, EMAIL_TO   (Gmail: use an App Password, not your login password)
    SMTP_HOST (default smtp.gmail.com)
    TG_TOKEN, TG_CHAT                          (optional Telegram)
The job data is written directly in the email / Telegram message body (no attachments).
Also writes per-city CSVs into history/<date>/ so you can open just one city in Excel.
"""
import argparse, csv, html, os, re, shutil, smtplib, sys
from collections import defaultdict
from datetime import date
from email.message import EmailMessage

BUCKETS = [
    ("Bengaluru", r"bengaluru|bangalore"),
    ("Hyderabad", r"hyderabad|secunderabad"),
    ("Pune", r"\bpune\b"),
    ("Mumbai", r"mumbai|thane"),
    ("Delhi NCR", r"delhi|gurgaon|gurugram|noida|ghaziabad|faridabad"),
    ("Chennai", r"chennai"),
    ("Remote", r"remote|work from home|wfh|anywhere"),
]
OTHER = "Other / unspecified"


def bucket(loc):
    for name, pat in BUCKETS:
        if re.search(pat, loc or "", re.I):
            return name
    return OTHER


def load_env(path=".env"):
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def build_html(groups, total):
    parts = [f"<h2>{total} new jobs</h2>"]
    for city, rows in groups:
        parts.append(f"<h3>{html.escape(city)} ({len(rows)})</h3>"
                     "<table cellpadding='5' style='border-collapse:collapse;font:14px Arial'>")
        for r in rows:
            parts.append(
                "<tr style='border-bottom:1px solid #ddd'>"
                f"<td><b>{html.escape(r['company'])}</b></td>"
                f"<td><a href='{html.escape(r['url'])}'>{html.escape(r['title'])}</a></td>"
                f"<td>{html.escape(r['location'])}</td>"
                f"<td>{html.escape(r.get('posted', ''))}</td>"
                f"<td style='color:#777'>{html.escape(r.get('source', ''))}</td></tr>")
        parts.append("</table>")
    return "".join(parts)


def build_text(groups, total):
    out = [f"{total} new jobs\n"]
    for city, rows in groups:
        out.append(f"== {city} ({len(rows)}) ==")
        for r in rows:
            out.append(f"- {r['company']}: {r['title']} [{r['location']}]\n  {r['url']}")
        out.append("")
    return "\n".join(out)


def send_email(subject, text, html_body):
    user, pw, to = (os.environ.get(k) for k in ("EMAIL_USER", "EMAIL_APP_PASSWORD", "EMAIL_TO"))
    if not (user and pw and to):
        print("Email not configured (EMAIL_USER / EMAIL_APP_PASSWORD / EMAIL_TO) - skipping email.")
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), 465) as s:
        s.login(user, pw)
        s.send_message(msg)
    print(f"Email sent to {to}")


def chunk_text(text, limit=3800):
    """Split text into pieces under Telegram's 4096-char limit, breaking on blank lines/lines."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks


def send_telegram(text):
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if not (token and chat):
        return
    import requests
    base = f"https://api.telegram.org/bot{token}"
    for part in chunk_text(text):
        requests.post(f"{base}/sendMessage", timeout=20,
                      data={"chat_id": chat, "text": part, "disable_web_page_preview": "true"})
    print("Telegram sent")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="new_jobs.csv")
    ap.add_argument("--cities", help="comma-separated, e.g. Bengaluru,Remote")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    load_env()

    if not os.path.exists(a.csv):
        print("No new_jobs.csv - nothing to send.")
        return
    rows = list(csv.DictReader(open(a.csv, encoding="utf-8")))
    for r in rows:
        r["city"] = bucket(r.get("location", ""))
    if a.cities:
        want = {c.strip().lower() for c in a.cities.split(",")}
        rows = [r for r in rows if r["city"].lower() in want]
    if not rows:
        print("0 new jobs for the selected locations - nothing to send.")
        return

    by_city = defaultdict(list)
    for r in rows:
        by_city[r["city"]].append(r)
    order = [n for n, _ in BUCKETS] + [OTHER]
    groups = [(c, sorted(by_city[c], key=lambda r: r["company"].lower())) for c in order if c in by_city]
    total = len(rows)

    text, html_body = build_text(groups, total), build_html(groups, total)
    if a.dry_run:
        print(text)
        return

    outdir = os.path.join("history", date.today().isoformat())
    os.makedirs(outdir, exist_ok=True)
    fields = ["company", "title", "location", "posted", "source", "url"]
    for city, rs in groups:
        fn = os.path.join(outdir, re.sub(r"[^A-Za-z0-9]+", "_", city) + ".csv")
        with open(fn, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rs)
    shutil.copy(a.csv, os.path.join("history", f"new_jobs_{date.today().isoformat()}.csv"))

    try:
        send_email(f"{total} new jobs - {date.today():%d %b}", text, html_body)
    except Exception as e:
        print("Email failed:", type(e).__name__, e)
    try:
        send_telegram(text)
    except Exception as e:
        print("Telegram failed:", type(e).__name__, e)
    print(f"Per-city CSVs saved in {outdir}/")


if __name__ == "__main__":
    main()
