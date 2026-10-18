#!/usr/bin/env python3
"""
jobs_boards.py - LinkedIn / Indeed / Naukri (and optionally Glassdoor) via python-jobspy.
Shares seen_jobs.json and appends to new_jobs.csv, same as job_scraper_v2.py / crawler.py.
When a board exposes the company's own apply link (job_url_direct), that link is saved.

LinkedIn is kept deliberately small (few searches, long pauses) to avoid IP blocks.

Needs Python 3.10+ :   pip install python-jobspy requests
Run:   python3.11 jobs_boards.py
       python3.11 jobs_boards.py --hours 24 --sites linkedin,naukri
"""
import argparse, csv, os, random, time
from datetime import date
import job_scraper_v2 as v2

# LinkedIn runs at most once per calendar day (local date stored here)
LINKEDIN_FLAG = "linkedin_last_run.txt"

# ---- LinkedIn: small and polite ----
LINKEDIN_SEARCHES = ["software engineer java", "software engineer react",
                     "software engineer python", "SDE 1", "SDE 2"]
LINKEDIN_LOCATIONS = ["Bengaluru, India"]
LINKEDIN_RESULTS = 20

# ---- Other boards: can afford the full list ----
OTHER_SEARCHES = ["software engineer java", "software engineer react", "software engineer python",
                  "software engineer javascript", "SDE 1", "SDE 2", "software developer",
                  "full stack developer", "backend developer java", "frontend developer react"]
OTHER_LOCATIONS = ["Bengaluru, India", "Hyderabad, India", "Pune, India", "Remote, India"]
OTHER_RESULTS = 30


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=48, help="only jobs posted within N hours")
    ap.add_argument("--sites", default="linkedin,indeed,naukri")
    ap.add_argument("--force", action="store_true", help="run LinkedIn even if already done today")
    a = ap.parse_args()
    try:
        from jobspy import scrape_jobs
    except ImportError:
        raise SystemExit("Install with Python 3.10+: pip install python-jobspy")

    sites = [s.strip() for s in a.sites.split(",")]
    today = date.today().isoformat()
    last = open(LINKEDIN_FLAG).read().strip() if os.path.exists(LINKEDIN_FLAG) else ""
    if "linkedin" in sites and last == today and not a.force:
        print(f"LinkedIn already searched today ({today}) - skipping it. Use --force to override.")
        sites = [x for x in sites if x != "linkedin"]
    linkedin_ok = 0
    seen = set(v2.load(v2.SEEN_FILE, []))
    found = {}

    def keep(r):
        title = str(r.get("title") or "")
        board_url = str(r.get("job_url") or "")
        if not board_url or board_url in found:
            return
        if not v2.LEVEL_OK.search(title) or v2.LEVEL_BAD.search(title):
            return
        desc = str(r.get("description") or "")
        if desc and desc != "nan" and not v2.TECH.search(title + " " + desc[:4000]):
            return
        direct = r.get("job_url_direct")
        direct = str(direct) if direct and str(direct) != "nan" else ""
        found[board_url] = dict(
            company=str(r.get("company") or ""), title=title,
            location=str(r.get("location") or ""),
            posted=str(r.get("date_posted") or "")[:10],
            source=f'{r.get("site")}{"-direct" if direct else ""}',
            url=direct or board_url)

    plans = []
    if "linkedin" in sites:
        plans.append((["linkedin"], LINKEDIN_SEARCHES, LINKEDIN_LOCATIONS, LINKEDIN_RESULTS, (8, 15)))
    rest = [s for s in sites if s != "linkedin"]
    if rest:
        plans.append((rest, OTHER_SEARCHES, OTHER_LOCATIONS, OTHER_RESULTS, (3, 6)))

    for site_list, searches, locations, n, (lo, hi) in plans:
        blocked = False
        for loc in locations:
            for term in searches:
                if blocked:
                    break
                try:
                    df = scrape_jobs(site_name=site_list, search_term=term, location=loc,
                                     results_wanted=n, hours_old=a.hours,
                                     country_indeed="India", linkedin_fetch_description=False)
                except Exception as e:
                    msg = str(e)
                    print(f"  skip '{term}' @ {loc}: {type(e).__name__}: {msg[:120]}")
                    if "linkedin" in site_list and ("429" in msg or "too many" in msg.lower()):
                        print("  LinkedIn is throttling - skipping it for this run.")
                        blocked = True
                    time.sleep(5)
                    continue
                if "linkedin" in site_list:
                    linkedin_ok += 1
                for _, r in df.iterrows():
                    keep(r)
                print(f"{'+'.join(site_list)} | {loc} | {term}: total kept {len(found)}", flush=True)
                time.sleep(random.uniform(lo, hi))

    if "linkedin" in sites:
        if linkedin_ok:
            open(LINKEDIN_FLAG, "w").write(today)
            print(f"LinkedIn done for {today} ({linkedin_ok} searches succeeded); won't run again today.")
        else:
            print("LinkedIn returned nothing this run (likely blocked); flag NOT set. "
                  "Wait a few hours before retrying.")

    new = {k: j for k, j in found.items() if k not in seen}
    exists = os.path.exists("new_jobs.csv")
    with open("new_jobs.csv", "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["company", "title", "location", "posted", "source", "url"])
        for j in new.values():
            w.writerow([j["company"], j["title"], j["location"], j["posted"], j["source"], j["url"]])
    v2.json.dump(sorted(seen | set(found)), open(v2.SEEN_FILE, "w"))
    direct_n = sum(1 for j in new.values() if j["source"].endswith("-direct"))
    print(f"{len(found)} matching, {len(new)} new appended ({direct_n} with company-direct links)")


if __name__ == "__main__":
    main()
