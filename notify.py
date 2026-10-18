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
    Other SMTP providers (Brevo etc.): SMTP_HOST, SMTP_USER (login), EMAIL_FROM (verified sender), SMTP_PORT
The jobs are sent as CSV text (company,title,location,posted,source,url) in the email /
Telegram message body - no attachments.
Also writes per-city CSVs into history/<date>/ so you can open just one city in Excel.
"""
import argparse, csv, html, os, re, shutil, smtplib, sys, time
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


CSV_FIELDS = ["company", "title", "location", "posted", "source", "url"]


def build_text(groups, total):
    """All jobs as CSV text: company,title,location,posted,source,url (grouped by city order)."""
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_FIELDS)
    for city, rows in groups:
        for r in rows:
            w.writerow([r.get(k, "") or "" for k in CSV_FIELDS])
    return buf.getvalue()


def parse_recipients(value):
    return [x.strip() for x in re.split(r"[,;\s]+", value or "") if x.strip()]


def build_markdown(groups, total):
    out = [f"# {total} new jobs - {date.today():%d %b %Y}\n"]
    for city, rows in groups:
        out.append(f"## {city} ({len(rows)})\n")
        for r in rows:
            posted = f" - {r['posted']}" if r.get("posted") else ""
            out.append(f"- **{r['company']}** - [{r['title']}]({r['url']}) - {r['location']}{posted}")
        out.append("")
    return "\n".join(out)


def send_email(subject, text, html_body=None):
    env = os.environ.get
    from_addr = (env("EMAIL_FROM") or env("EMAIL_USER") or "").strip()
    login_user = (env("SMTP_USER") or env("EMAIL_USER") or "").strip()
    pw = (env("EMAIL_APP_PASSWORD") or "").replace(" ", "").strip()
    to = env("EMAIL_TO")
    if not (from_addr and login_user and pw and to):
        print("Email not configured (EMAIL_USER / EMAIL_APP_PASSWORD / EMAIL_TO) - skipping email.")
        return True
    recipients = parse_recipients(to)
    if not recipients:
        print("EMAIL_TO has no valid addresses.")
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, from_addr, ", ".join(recipients)
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    host = env("SMTP_HOST") or "smtp.gmail.com"
    port = (env("SMTP_PORT") or "").strip()
    if port == "465":
        modes = ["ssl465"]
    elif port == "587":
        modes = ["starttls587"]
    elif host.endswith("gmail.com"):
        modes = ["ssl465", "starttls587"]
    else:
        modes = ["starttls587", "ssl465"]

    for attempt in range(1, 4):
        for mode in modes:
            try:
                if mode == "ssl465":
                    server = smtplib.SMTP_SSL(host, 465, timeout=30)
                else:
                    server = smtplib.SMTP(host, 587, timeout=30)
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                with server:
                    server.login(login_user, pw)
                    server.send_message(msg, to_addrs=recipients)
                print(f"Email sent to {', '.join(recipients)} (via {host} {mode})")
                return True
            except smtplib.SMTPAuthenticationError as e:
                print("SMTP server rejected the login. Check the username/password secrets.", e)
                return False
            except Exception as e:
                print(f"  attempt {attempt} via {host} {mode} failed: {type(e).__name__}: {e}")
        time.sleep(5)
    return False


def chunk_text(text, limit=3800):
    """Split into pieces under Telegram's 4096-char limit. If the text starts with the CSV
    header, repeat that header at the top of every piece."""
    first, _, rest = text.partition("\n")
    header = first if first.startswith("company,") else None
    lines = rest.split("\n") if header else text.split("\n")
    base = (header + "\n") if header else ""
    chunks, cur = [], base
    for line in lines:
        if not line:
            continue
        if len(cur) + len(line) + 1 > limit and cur.strip() != base.strip():
            chunks.append(cur)
            cur = base
        cur += line + "\n"
    if cur.strip() and cur.strip() != base.strip():
        chunks.append(cur)
    return chunks


def send_telegram(text):
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if not (token and chat):
        return True
    import requests
    base = f"https://api.telegram.org/bot{token.strip()}"
    ok = True
    for part in chunk_text(text):
        r = requests.post(f"{base}/sendMessage", timeout=20,
                          data={"chat_id": chat.strip(), "text": part,
                                "disable_web_page_preview": "true"})
        if not r.ok:
            ok = False
            print(f"Telegram error {r.status_code}: {r.text[:200]}")
            break
    if ok:
        print("Telegram sent")
    return ok


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

    text = build_text(groups, total)
    if a.dry_run:
        print(text)
        return

    with open("message.md", "w", encoding="utf-8") as f:     # used by the GitHub Issue step
        f.write(build_markdown(groups, total))

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

    email_ok = True
    try:
        counts = ", ".join(f"{c} {len(r)}" for c, r in groups)
        email_ok = send_email(f"{total} new jobs - {date.today():%d %b} ({counts})", text)
    except Exception as e:
        email_ok = False
        print("Email failed:", type(e).__name__, e)
    tg_ok = True
    try:
        tg_ok = send_telegram(text)
    except Exception as e:
        tg_ok = False
        print("Telegram failed:", type(e).__name__, e)
    tg_configured = bool(os.environ.get("TG_TOKEN") and os.environ.get("TG_CHAT"))
    email_configured = bool(os.environ.get("EMAIL_USER") and os.environ.get("EMAIL_APP_PASSWORD")
                            and os.environ.get("EMAIL_TO"))
    # run counts as delivered if ANY configured channel worked
    delivered = (email_configured and email_ok) or (tg_configured and tg_ok) \
        or not (email_configured or tg_configured)
    email_ok = delivered
    print(f"Per-city CSVs saved in {outdir}/")
    if not email_ok:
        sys.exit("DELIVERY FAILED on every configured channel - exiting with error so this run's "
                 "state is not saved and these jobs are re-sent next time.")


if __name__ == "__main__":
    main()
