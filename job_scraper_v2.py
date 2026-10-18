#!/usr/bin/env python3
"""
Job scraper v2 - covers far more of the companies in
https://github.com/krshubham-1514/top-companies-career-pages

Stages (each only runs for companies the previous stage could not resolve):
  1. Fetch career page HTML, look for an ATS link.
  2. Probe free public ATS APIs by guessing the company's board slug
     (Greenhouse / Lever / Ashby / Workable / Recruitee / Breezy).
  3. (--browser) Open the page in headless Chromium, like a real visitor:
     - find ATS links/API calls that only appear after JavaScript runs
       (incl. Workday), or
     - read job links / JSON the page loads itself (generic fallback).

Setup (Python 3.9+):
    pip3 install requests
    # only for --browser:
    pip3 install playwright && python3 -m playwright install chromium

Run:
    python3 job_scraper_v2.py                # stages 1-2 (fast)
    python3 job_scraper_v2.py --browser      # + stage 3 (slower, best coverage)
    python3 job_scraper_v2.py --days 7 --all-locations
Output: new_jobs.csv (only jobs not seen in earlier runs), unresolved.txt
"""
import argparse, asyncio, csv, json, os, re, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
import requests

README_URL = ("https://raw.githubusercontent.com/krshubham-1514/"
              "top-companies-career-pages/main/README.md")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TIMEOUT = 15
CACHE_FILE, SEEN_FILE = "ats_cache_v2.json", "seen_jobs.json"
UTC = timezone.utc

# ----------------------- filters (edit freely) -----------------------
LEVEL_OK = re.compile(
    r"\b(sde|swe)[\s\-]*(1|2|i|ii)\b|software (development )?engineer[\s,\-]*(1|2|i|ii)\b|"
    r"\bsoftware (developer|engineer)\b|\bfull[\s\-]?stack\b|\bfront[\s\-]?end\b|"
    r"\bback[\s\-]?end\b|\bmember of technical staff\b|\bassociate software\b|\bmts\b|"
    r"\b(java|python|react|node|javascript)\b.*\b(developer|engineer)\b", re.I)
LEVEL_BAD = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|head|architect|intern|"
    r"vp|iii|iv|3|4|5)\b", re.I)
TECH = re.compile(r"\b(java|react(\.?js)?|python|javascript|typescript|node(\.?js)?|spring)\b", re.I)
LOCATION = re.compile(r"india|bengaluru|bangalore|hyderabad|pune|remote|mumbai|delhi|gurgaon|"
                      r"gurugram|noida|chennai|\d+\s+locations?|multiple", re.I)
# ---------------------------------------------------------------------

PATTERNS = {
    "greenhouse": re.compile(r"(?:boards-api\.greenhouse\.io/v1/boards/|(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?)([\w\-]+)", re.I),
    "lever": re.compile(r"(?:jobs\.lever\.co/|api\.lever\.co/v0/postings/)([\w\-]+)", re.I),
    "ashby": re.compile(r"(?:jobs\.ashbyhq\.com/|api\.ashbyhq\.com/posting-api/job-board/)([\w\-\.]+)", re.I),
    "smartrecruiters": re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([\w\-]+)", re.I),
    "workable": re.compile(r"apply\.workable\.com/(?:api/v\d/accounts/)?([\w\-]+)", re.I),
    "recruitee": re.compile(r"([\w\-]+)\.recruitee\.com", re.I),
    "breezy": re.compile(r"([\w\-]+)\.breezy\.hr", re.I),
}
WORKDAY = re.compile(r"https?://([\w\-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:wday/cxs/[\w\-]+/)?(?:[a-z]{2}-[A-Z]{2}/)?([\w\-]+)", re.I)
BAD_SLUGS = {"embed", "js", "css", "static", "api", "www", "v1", "v3", "wday", "cxs", "assets", "jobs"}


def detect_in_blob(blob):
    m = WORKDAY.search(blob)
    if m and m.group(3).lower() not in BAD_SLUGS:
        return {"ats": "workday", "slug": f"{m.group(1)}|{m.group(2)}|{m.group(3)}"}
    for name, pat in PATTERNS.items():
        for m in pat.finditer(blob):
            if m.group(1).lower() not in BAD_SLUGS:
                return {"ats": name, "slug": m.group(1)}
    return None


# ---------------- stage 1: static HTML ----------------
def detect_static(url):
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        return detect_in_blob(r.url + "\n" + r.text)
    except Exception:
        return None


# ---------------- stage 2: slug probing ----------------
def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def slug_candidates(name, url):
    host = urlparse(url).netloc.lower().replace("www.", "")
    labels = host.split(".")
    dom = labels[-2] if len(labels) >= 2 and labels[-2] not in ("co", "com") else labels[0]
    clean = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    cands = [dom, clean.replace(" ", ""), clean.replace(" ", "-"), clean.split(" ")[0]]
    seen, out = set(), []
    for c in cands:
        if c and len(c) > 2 and c not in seen:
            seen.add(c); out.append(c)
    return out


def jget(url):
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    return r.json() if r.status_code == 200 else None


def probe(name, url):
    for s in slug_candidates(name, url):
        try:
            d = jget(f"https://boards-api.greenhouse.io/v1/boards/{s}")
            if isinstance(d, dict) and d.get("name"):
                a, b = norm(d["name"]), norm(name)
                if a in b or b in a or norm(s) in b:       # name sanity check
                    return {"ats": "greenhouse", "slug": s, "guessed": True}
        except Exception:
            pass
        for ats, u in (("lever", f"https://api.lever.co/v0/postings/{s}?mode=json&limit=1"),
                       ("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{s}")):
            try:
                d = jget(u)
                if (ats == "lever" and isinstance(d, list) and d) or \
                   (ats == "ashby" and isinstance(d, dict) and d.get("jobs")):
                    return {"ats": ats, "slug": s, "guessed": True}
            except Exception:
                pass
    return None


# ---------------- ATS fetchers ----------------
def parse_dt(v):
    if not v:
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000, tz=UTC)
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except Exception:
        return None


def workday_posted(s):
    s = (s or "").lower()
    now = datetime.now(UTC)
    if "today" in s: return now
    if "yesterday" in s: return now - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*day", s)
    return now - timedelta(days=int(m.group(1))) if m else None


def fetch_jobs(company, info):
    ats, slug = info["ats"], info["slug"]
    J = []
    if ats == "greenhouse":
        d = jget(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true") or {}
        for j in d.get("jobs", []):
            J.append(dict(title=j["title"], location=(j.get("location") or {}).get("name", ""),
                          url=j["absolute_url"], posted=parse_dt(j.get("updated_at")),
                          text=re.sub("<[^>]+>", " ", j.get("content", ""))))
    elif ats == "lever":
        d = jget(f"https://api.lever.co/v0/postings/{slug}?mode=json")
        for j in (d if isinstance(d, list) else []):
            J.append(dict(title=j["text"], location=(j.get("categories") or {}).get("location", ""),
                          url=j["hostedUrl"], posted=parse_dt(j.get("createdAt")),
                          text=j.get("descriptionPlain", "")))
    elif ats == "ashby":
        d = jget(f"https://api.ashbyhq.com/posting-api/job-board/{slug}") or {}
        for j in d.get("jobs", []):
            J.append(dict(title=j["title"], location=j.get("location", ""), url=j.get("jobUrl", ""),
                          posted=parse_dt(j.get("publishedAt")), text=j.get("descriptionPlain", "")))
    elif ats == "smartrecruiters":
        d = jget(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100") or {}
        for j in d.get("content", []):
            loc = j.get("location", {})
            J.append(dict(title=j["name"], location=f'{loc.get("city","")}, {loc.get("country","")}',
                          url=f'https://jobs.smartrecruiters.com/{slug}/{j["id"]}',
                          posted=parse_dt(j.get("releasedDate")), text=""))
    elif ats == "workable":
        r = requests.post(f"https://apply.workable.com/api/v3/accounts/{slug}/jobs",
                          json={"query": "", "limit": 100}, headers=UA, timeout=TIMEOUT)
        for j in (r.json().get("results", []) if r.ok else []):
            loc = j.get("location") or {}
            J.append(dict(title=j["title"], location=f'{loc.get("city","")}, {loc.get("country","")}',
                          url=f'https://apply.workable.com/{slug}/j/{j["shortcode"]}/',
                          posted=parse_dt(j.get("published")), text=""))
    elif ats == "recruitee":
        d = jget(f"https://{slug}.recruitee.com/api/offers/") or {}
        for j in d.get("offers", []):
            J.append(dict(title=j["title"], location=j.get("location", ""), url=j.get("careers_url", ""),
                          posted=parse_dt(j.get("published_at")),
                          text=re.sub("<[^>]+>", " ", j.get("description", ""))))
    elif ats == "breezy":
        d = jget(f"https://{slug}.breezy.hr/json")
        for j in (d if isinstance(d, list) else []):
            loc = (j.get("location") or {}).get("name", "")
            J.append(dict(title=j["name"], location=loc, url=j.get("url", ""),
                          posted=parse_dt(j.get("published_date")), text=""))
    elif ats == "workday":
        tenant, wd, site = slug.split("|")
        api = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
        for q in ("software engineer", "software developer"):
            for off in range(0, 100, 20):
                r = requests.post(api, json={"appliedFacets": {}, "limit": 20, "offset": off,
                                             "searchText": q}, headers=UA, timeout=TIMEOUT)
                if not r.ok:
                    break
                posts = r.json().get("jobPostings", [])
                for j in posts:
                    J.append(dict(title=j.get("title", ""), location=j.get("locationsText", ""),
                                  url=f'https://{tenant}.{wd}.myworkdayjobs.com/{site}{j.get("externalPath","")}',
                                  posted=workday_posted(j.get("postedOn")), text=""))
                if len(posts) < 20:
                    break
    return [dict(j, company=company, ats=ats) for j in J]


# ---------------- stage 3: headless browser ----------------
TITLE_KEYS = ("title", "jobTitle", "job_title", "name", "position", "text", "posting_title")
URL_KEYS = ("url", "absolute_url", "applyUrl", "apply_url", "hostedUrl", "link", "jobUrl", "externalPath")


def walk_json(o, out, depth=0):
    if depth > 6:
        return
    if isinstance(o, list):
        for x in o:
            if isinstance(x, dict):
                t = next((x[k] for k in TITLE_KEYS if isinstance(x.get(k), str)), None)
                if t and 5 < len(t) < 150:
                    u = next((x[k] for k in URL_KEYS if isinstance(x.get(k), str)), "")
                    loc = x.get("location") or x.get("locations") or x.get("city") or ""
                    out.append((t, u, json.dumps(loc)[:100] if not isinstance(loc, str) else loc))
            walk_json(x, out, depth + 1)
    elif isinstance(o, dict):
        for v in o.values():
            walk_json(v, out, depth + 1)


async def browse_all(items, concurrency=6):
    from playwright.async_api import async_playwright
    results = {}
    sem = asyncio.Semaphore(concurrency)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        async def one(name, url):
            async with sem:
                ctx = await browser.new_context(user_agent=UA["User-Agent"])
                page = await ctx.new_page()
                seen_urls, jsons = [], []

                async def on_resp(resp):
                    seen_urls.append(resp.url)
                    if "json" in (resp.headers.get("content-type") or ""):
                        try:
                            jsons.append(await resp.json())
                        except Exception:
                            pass
                page.on("response", on_resp)
                try:
                    await page.goto(url, wait_until="networkidle", timeout=30000)
                    await page.wait_for_timeout(2000)
                    html = await page.content()
                    anchors = await page.eval_on_selector_all(
                        "a", "els=>els.map(e=>[(e.innerText||'').trim(), e.href])")
                    srcs = await page.eval_on_selector_all("iframe", "els=>els.map(e=>e.src)")
                    blob = "\n".join([page.url, html] + seen_urls + srcs + [a[1] for a in anchors])
                    info = detect_in_blob(blob)
                    if info:
                        results[name] = ("ats", info)
                    else:
                        jobs = []
                        for t, href in anchors:
                            if t and len(t) < 150 and LEVEL_OK.search(t):
                                jobs.append((t.split("\n")[0], href, ""))
                        for js in jsons:
                            walk_json(js, jobs)
                        results[name] = ("generic", jobs)
                except Exception as e:
                    results[name] = ("error", type(e).__name__)
                finally:
                    await ctx.close()
        await asyncio.gather(*[one(n, u) for n, u in items])
        await browser.close()
    return results


# ---------------- filtering / main ----------------
def matches(j, max_age, loc_on):
    t = j["title"]
    if not LEVEL_OK.search(t) or LEVEL_BAD.search(t):
        return False
    if j["text"] and not TECH.search(t + " " + j["text"][:4000]):
        return False
    if loc_on and j["location"] and not LOCATION.search(j["location"]):
        return False
    if j["posted"] and j["posted"] < datetime.now(UTC) - timedelta(days=max_age):
        return False
    return True


def load(path, default):
    return json.load(open(path)) if os.path.exists(path) else default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", action="store_true", help="use headless Chromium for unresolved sites")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--all-locations", action="store_true")
    a = ap.parse_args()

    md = requests.get(README_URL, headers=UA, timeout=TIMEOUT).text
    companies = re.findall(r"-\s+\*\*(.+?)\*\*:\s+\[Career Page\]\((.+?)\)", md)
    urls = dict(companies)
    print(f"{len(companies)} companies")
    cache = load(CACHE_FILE, {})

    # stage 1 + 2
    todo = [(n, u) for n, u in companies if n not in cache]
    if todo:
        print(f"Stage 1/2: detecting ATS for {len(todo)} companies...")

        def resolve(n, u):
            return detect_static(u) or probe(n, u)
        with ThreadPoolExecutor(16) as ex:
            futs = {ex.submit(resolve, n, u): n for n, u in todo}
            for f in as_completed(futs):
                try:
                    cache[futs[f]] = f.result() or {"ats": None}
                except Exception:
                    cache[futs[f]] = {"ats": None}
        json.dump(cache, open(CACHE_FILE, "w"), indent=1)

    # stage 3
    generic_jobs = []
    unresolved = [(n, urls[n]) for n, i in cache.items() if not i.get("ats") and n in urls]
    if a.browser and unresolved:
        try:
            print(f"Stage 3: browser pass over {len(unresolved)} sites (several minutes)...")
            res = asyncio.run(browse_all(unresolved))
            for n, (kind, val) in res.items():
                if kind == "ats":
                    cache[n] = val
                elif kind == "generic":
                    for t, u, loc in val:
                        generic_jobs.append(dict(company=n, title=t, location=loc, url=u or urls[n],
                                                 posted=None, text="", ats="browser"))
            json.dump(cache, open(CACHE_FILE, "w"), indent=1)
        except ImportError:
            print("Playwright not installed: pip3 install playwright && python3 -m playwright install chromium")

    supported = {n: i for n, i in cache.items() if i.get("ats")}
    print(f"{len(supported)} companies resolved to an ATS API")
    with open("unresolved.txt", "w") as f:
        for n, i in cache.items():
            if not i.get("ats"):
                f.write(f"{n}\t{urls.get(n,'')}\n")

    found = []
    with ThreadPoolExecutor(12) as ex:
        futs = {ex.submit(fetch_jobs, n, i): n for n, i in supported.items()}
        for f in as_completed(futs):
            try:
                found += f.result()
            except Exception as e:
                print("  skip", futs[f], type(e).__name__, file=sys.stderr)
    found += generic_jobs
    found = [j for j in found if matches(j, a.days, not a.all_locations)]

    seen = set(load(SEEN_FILE, []))
    new = [j for j in found if j["url"] not in seen]
    new.sort(key=lambda j: j["posted"] or datetime.min.replace(tzinfo=UTC), reverse=True)
    with open("new_jobs.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["company", "title", "location", "posted", "source", "url"])
        for j in new:
            w.writerow([j["company"], j["title"], j["location"],
                        j["posted"].date() if j["posted"] else "", j["ats"], j["url"]])
    json.dump(sorted(seen | {j["url"] for j in found}), open(SEEN_FILE, "w"))
    print(f"{len(found)} matching jobs, {len(new)} new -> new_jobs.csv")


if __name__ == "__main__":
    main()
