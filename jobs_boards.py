#!/usr/bin/env python3
"""
jobs_boards.py - LinkedIn / Indeed / Naukri (and optionally Glassdoor) via python-jobspy.
Shares seen_jobs.json and appends to new_jobs.csv, same as job_scraper_v2.py / crawler.py.
When a board exposes the company's own apply link (job_url_direct), that link is saved.

Designed to NEVER fail the pipeline: every error is caught, reported to the user
(Telegram and/or email), and the script exits 0 so later steps still run.

Needs Python 3.10+ :   pip install python-jobspy requests
Run:   python3.11 jobs_boards.py
       python3.11 jobs_boards.py --hours 24 --sites linkedin,naukri
"""
import argparse, csv, json, os, random, smtplib, time, traceback
from datetime import date
from email.message import EmailMessage

LINKEDIN_FLAG = "linkedin_last_run.txt"

LINKEDIN_SEARCHES = ["software engineer java", "software engineer react",
                     "software engineer python", "SDE 1", "SDE 2"]
LINKEDIN_LOCATIONS = ["Bengaluru, India"]
LINKEDIN_RESULTS = 20

OTHER_SEARCHES = ["software engineer java", "software engineer react", "software engineer python",
                  "software engineer javascript", "SDE 1", "SDE 2", "software developer",
                  "full stack developer", "backend developer java", "frontend developer react"]
OTHER_LOCATIONS = ["Bengaluru, India", "Hyderabad, India", "Pune, India", "Remote, India"]
OTHER_RESULTS = 30

MAX_CONSECUTIVE_FAILS = 5

errors = []  # collected and sent to the user at the end


# ---------------------------------------------------------------- notifications
def log_error(msg):
    print(f"  [ERROR] {msg}", flush=True)
    errors.append(msg)


def send_telegram(text):
    token, chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT")
    if not (token and chat):
        return False
    try:
        import requests
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": text[:4000]}, timeout=20)
        return r.ok
    except Exception as e:
        print(f"  (telegram notify failed: {e})")
        return False


def send_email(subject, body):
    user = os.getenv("SMTP_USER") or os.getenv("EMAIL_USER")
    pwd = os.getenv("EMAIL_APP_PASSWORD")
    to = os.getenv("EMAIL_TO")
    if not (user and pwd and to):
        return False
    host = os.getenv("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.getenv("SMTP_PORT") or 587)
    sender = os.getenv("EMAIL_FROM") or os.getenv("EMAIL_USER") or user
    try:
        m = EmailMessage()
        m["Subject"], m["From"], m["To"] = subject, sender, to
        m.set_content(body)
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, pwd)
            s.send_message(m)
        return True
    except Exception as e:
        print(f"  (email notify failed: {e})")
        return False


def notify_errors():
    if not errors:
        return
    body = "jobs_boards.py had problems (the workflow continued anyway):\n\n" + \
           "\n".join(f"- {e}" for e in errors[:25])
    sent = send_telegram("⚠️ " + body)
    sent = send_email("Job bot: jobs_boards.py warnings", body) or sent
    if not sent:
        print("No notification channel configured/working; errors are only in the log.")


# ---------------------------------------------------------------- helpers
def clean(v):
    """Turn None / NaN / 'nan' into an empty string."""
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "none", "nat") else s


def load_seen(v2):
    try:
        return set(v2.load(v2.SEEN_FILE, []))
    except Exception as e:
        log_error(f"Could not load seen file: {type(e).__name__}: {e}")
        return set()


# ---------------------------------------------------------------- main logic
def run(a, state):
    """Does the scraping. Results are stored in `state` so they survive any exception."""
    try:
        import job_scraper_v2 as v2
    except Exception as e:
        log_error(f"Cannot import job_scraper_v2: {type(e).__name__}: {e}")
        return
    state["v2"] = v2

    try:
        from jobspy import scrape_jobs
    except ImportError:
        log_error("python-jobspy is not installed. Add `pip install python-jobspy` "
                  "to the workflow's Install step (needs Python 3.10+).")
        return

    sites = [s.strip() for s in a.sites.split(",") if s.strip()]
    today = date.today().isoformat()
    try:
        last = open(LINKEDIN_FLAG).read().strip() if os.path.exists(LINKEDIN_FLAG) else ""
    except Exception:
        last = ""
    if "linkedin" in sites and last == today and not a.force:
        print(f"LinkedIn already searched today ({today}) - skipping it. Use --force to override.")
        sites = [x for x in sites if x != "linkedin"]

    state["seen"] = load_seen(v2)
    found = state["found"]
    linkedin_ok = 0
    deadline = time.time() + a.max_minutes * 60

    def keep(r):
        try:
            title = clean(r.get("title"))
            board_url = clean(r.get("job_url"))
            if not board_url or board_url in found:
                return
            if not v2.LEVEL_OK.search(title) or v2.LEVEL_BAD.search(title):
                return
            desc = clean(r.get("description"))
            if desc and not v2.TECH.search(title + " " + desc[:4000]):
                return
            direct = clean(r.get("job_url_direct"))
            found[board_url] = dict(
                company=clean(r.get("company")), title=title,
                location=clean(r.get("location")),
                posted=clean(r.get("date_posted"))[:10],
                source=f'{clean(r.get("site"))}{"-direct" if direct else ""}',
                url=direct or board_url)
        except Exception as e:
            log_error(f"Skipped one bad row: {type(e).__name__}: {e}")

    plans = []
    if "linkedin" in sites:
        plans.append((["linkedin"], LINKEDIN_SEARCHES, LINKEDIN_LOCATIONS, LINKEDIN_RESULTS, (8, 15)))
    rest = [s for s in sites if s != "linkedin"]
    if rest:
        plans.append((rest, OTHER_SEARCHES, OTHER_LOCATIONS, OTHER_RESULTS, (3, 6)))

    for site_list, searches, locations, n, (lo, hi) in plans:
        label = "+".join(site_list)
        fails_in_row = 0
        stop_plan = False
        for loc in locations:
            if stop_plan:
                break
            for term in searches:
                if time.time() > deadline:
                    log_error(f"{label}: time budget of {a.max_minutes} min reached; stopped early.")
                    stop_plan = True
                    break
                try:
                    df = scrape_jobs(site_name=site_list, search_term=term, location=loc,
                                     results_wanted=n, hours_old=a.hours,
                                     country_indeed="India", linkedin_fetch_description=False)
                except Exception as e:
                    msg = str(e)
                    print(f"  skip '{term}' @ {loc}: {type(e).__name__}: {msg[:120]}")
                    fails_in_row += 1
                    if "linkedin" in site_list and ("429" in msg or "too many" in msg.lower()):
                        log_error("LinkedIn is throttling (429) - skipped for the rest of this run.")
                        stop_plan = True
                        break
                    if fails_in_row >= MAX_CONSECUTIVE_FAILS:
                        log_error(f"{label}: {fails_in_row} failures in a row "
                                  f"(last: {type(e).__name__}: {msg[:150]}); giving up on it.")
                        stop_plan = True
                        break
                    time.sleep(5)
                    continue

                fails_in_row = 0
                if "linkedin" in site_list:
                    linkedin_ok += 1
                try:
                    if df is not None and len(df):
                        for _, r in df.iterrows():
                            keep(r)
                except Exception as e:
                    log_error(f"{label}: could not process results: {type(e).__name__}: {e}")
                print(f"{label} | {loc} | {term}: total kept {len(found)}", flush=True)
                time.sleep(random.uniform(lo, hi))

    if "linkedin" in sites:
        if linkedin_ok:
            try:
                open(LINKEDIN_FLAG, "w").write(today)
                print(f"LinkedIn done for {today} ({linkedin_ok} searches succeeded); won't run again today.")
            except Exception as e:
                log_error(f"Could not write LinkedIn flag: {e}")
        else:
            print("LinkedIn returned nothing this run (likely blocked); flag NOT set. "
                  "Wait a few hours before retrying.")


def save(state):
    """Always called, even after errors, so partial results are kept."""
    found, seen, v2 = state["found"], state["seen"], state.get("v2")
    if v2 is None:      # setup failed, nothing to save
        return
    new = {k: j for k, j in found.items() if k not in seen}
    try:
        exists = os.path.exists("new_jobs.csv")
        with open("new_jobs.csv", "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(["company", "title", "location", "posted", "source", "url"])
            for j in new.values():
                w.writerow([j["company"], j["title"], j["location"], j["posted"], j["source"], j["url"]])
    except Exception as e:
        log_error(f"Could not write new_jobs.csv: {type(e).__name__}: {e}")
    try:
        with open(v2.SEEN_FILE, "w") as f:
            json.dump(sorted(seen | set(found)), f)
    except Exception as e:
        log_error(f"Could not write seen file: {type(e).__name__}: {e}")
    direct_n = sum(1 for j in new.values() if j["source"].endswith("-direct"))
    print(f"{len(found)} matching, {len(new)} new appended ({direct_n} with company-direct links)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=48, help="only jobs posted within N hours")
    ap.add_argument("--sites", default="linkedin,indeed,naukri")
    ap.add_argument("--force", action="store_true", help="run LinkedIn even if already done today")
    ap.add_argument("--max-minutes", type=int, default=25, help="time budget for scraping")
    a, _ = ap.parse_known_args()

    state = {"found": {}, "seen": set(), "v2": None}
    try:
        run(a, state)
    except BaseException as e:   # includes KeyboardInterrupt/SystemExit: never fail the pipeline
        log_error(f"Unexpected {type(e).__name__}: {e}\n{traceback.format_exc()[-800:]}")
    finally:
        try:
            save(state)
        except Exception as e:
            log_error(f"Save step crashed: {type(e).__name__}: {e}")
        try:
            notify_errors()
        except Exception as e:
            print(f"notify crashed: {e}")


if __name__ == "__main__":
    main()
    raise SystemExit(0)   # always succeed; problems are reported via notification
