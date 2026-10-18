#!/usr/bin/env python3
"""
crawler.py - generic career-page crawler for companies job_scraper_v2.py could not
resolve to an ATS API. Drives headless Chromium like a visitor:
  open page -> click through to the job list -> search "software engineer" ->
  scroll / "load more" / "next" -> collect job links -> keep only NEW software roles.

Run AFTER job_scraper_v2.py (it reuses ats_cache_v2.json, seen_jobs.json, new_jobs.csv):
    python3 job_scraper_v2.py
    python3 crawler.py                       # all unresolved companies
    python3 crawler.py --only "Swiggy,Zomato" # test a few first
    python3 crawler.py --concurrency 4 --limit 50

Needs: pip3 install requests playwright && python3 -m playwright install chromium
Outputs: new_jobs.csv (appended), crawl_report.csv (per-company diagnostics)
"""
import argparse, asyncio, csv, json, os, re, sys
from urllib.parse import urljoin
import requests
import job_scraper_v2 as v2

JOB_URL = re.compile(
    r"/(job|jobs|position|positions|opening|openings|vacancy|vacancies|requisition|"
    r"requisitions|jobdetail|job-detail|job-posting|jobposting|opportunity|opportunities|apply)"
    r"/[^/?#]{3,}|[?&](jobid|job_id|jid|reqid|requisitionid|jobreqid)=", re.I)
HOP = re.compile(r"(view|see|search|browse|explore|find|open|current|all)\W+(all\W+)?"
                 r"(jobs|openings|positions|roles|opportunities|vacancies)|"
                 r"^(jobs|openings|open positions|current openings|search jobs|job search)$", re.I)
LOAD_MORE = re.compile(r"(load|show|view|see)\s+more", re.I)
NEXT = re.compile(r"^\s*(next|next page|›|»|>)\s*$", re.I)
LOOSE = re.compile(r"\b(software|sde|swe|developer|programmer|full[\s\-]?stack|front[\s\-]?end|"
                   r"back[\s\-]?end|engineer|technical staff|mts)\b", re.I)
NONSW = re.compile(r"\b(mechanical|electrical|civil|hardware|sales|customer|field|manufactur\w*|"
                   r"embedded|firmware|verification|sap|mainframe|chemical|process|plant)\b", re.I)
SEARCH_BOX = ('input[type="search"], input[placeholder*="search" i], input[placeholder*="keyword" i], '
              'input[placeholder*="job" i], input[name*="keyword" i], input[name*="search" i]')


def title_ok(title, location):
    if not LOOSE.search(title) or NONSW.search(title) or v2.LEVEL_BAD.search(title):
        return False
    if location and v2.LOCATION and not v2.LOCATION.search(location):
        return False
    return True


async def collect(page, store):
    try:
        anchors = await page.eval_on_selector_all(
            "a", "els=>els.map(e=>[(e.innerText||'').trim(), e.href])")
    except Exception:
        return
    for t, h in anchors:
        if not h.startswith("http") or not t:
            continue
        lines = [x.strip() for x in t.split("\n") if x.strip()]
        first = lines[0] if lines else ""
        if 4 < len(first) < 140 and (JOB_URL.search(h) or v2.LEVEL_OK.search(first)):
            store[h] = (first, " ".join(lines[1:3]))


async def try_click(page, locator):
    try:
        if await locator.count() and await locator.first.is_visible():
            await locator.first.click(timeout=4000)
            await page.wait_for_timeout(2500)
            return True
    except Exception:
        pass
    return False


async def crawl_one(browser, name, url, cache):
    ctx = await browser.new_context(user_agent=v2.UA["User-Agent"], viewport={"width": 1366, "height": 900})
    page = await ctx.new_page()
    resp_urls, jsons = [], []

    async def on_resp(r):
        resp_urls.append(r.url)
        if "json" in (r.headers.get("content-type") or ""):
            try:
                jsons.append(await r.json())
            except Exception:
                pass
    page.on("response", on_resp)
    links = {}
    status = "ok"
    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(1500)
        await collect(page, links)

        # hop to the real job list if this looks like a landing page
        hops = 0
        while len([h for h in links if JOB_URL.search(h)]) < 3 and hops < 2:
            anchors = await page.eval_on_selector_all(
                "a", "els=>els.map(e=>[(e.innerText||'').trim(), e.href])")
            nxt = next((h for t, h in anchors if h.startswith("http") and HOP.search(t.strip())
                        and h.split("#")[0] != page.url.split("#")[0]), None)
            if not nxt:
                break
            try:
                await page.goto(nxt, wait_until="networkidle", timeout=30000)
                await page.wait_for_timeout(1500)
            except Exception:
                break
            await collect(page, links)
            hops += 1

        # search for the role if the site has a search box
        box = page.locator(SEARCH_BOX)
        try:
            if await box.count() and await box.first.is_visible():
                await box.first.fill("software engineer", timeout=4000)
                await box.first.press("Enter")
                await page.wait_for_timeout(3500)
        except Exception:
            pass

        # scroll, load more, paginate
        for _ in range(6):
            await collect(page, links)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1000)
            clicked = await try_click(page, page.get_by_role("button", name=LOAD_MORE)) or \
                await try_click(page, page.get_by_role("link", name=LOAD_MORE)) or \
                await try_click(page, page.get_by_role("link", name=NEXT)) or \
                await try_click(page, page.get_by_role("button", name=NEXT))
            if not clicked:
                break
        await collect(page, links)

        html = await page.content()
        blob = "\n".join([page.url, html] + resp_urls + list(links))
        info = v2.detect_in_blob(blob)
        jobs = []
        if info:                                    # JS revealed a known ATS: use its API
            cache[name] = info
            try:
                jobs = [j for j in v2.fetch_jobs(name, info)
                        if v2.matches(j, 14, True)]
                status = f"ats:{info['ats']}"
            except Exception:
                status = f"ats:{info['ats']}-fetch-failed"
        if not jobs:
            for h, (t, loc) in links.items():
                if title_ok(t, loc):
                    jobs.append(dict(company=name, title=t, location=loc, url=h,
                                     posted=None, text="", ats="crawler"))
            extra = []
            for js in jsons:
                v2.walk_json(js, extra)
            for t, u, loc in extra:
                if title_ok(t, loc):
                    full = urljoin(page.url, u) if u else page.url
                    jobs.append(dict(company=name, title=t, location=loc, url=full,
                                     posted=None, text="", ats="crawler-json"))
        return jobs, status, len(links)
    except Exception as e:
        return [], f"error:{type(e).__name__}", len(links)
    finally:
        await ctx.close()


async def crawl_all(targets, cache, concurrency):
    from playwright.async_api import async_playwright
    sem = asyncio.Semaphore(concurrency)
    out, done = {}, [0]
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        async def worker(n, u):
            async with sem:
                try:
                    res = await asyncio.wait_for(crawl_one(browser, n, u, cache), timeout=120)
                except asyncio.TimeoutError:
                    res = ([], "timeout", 0)
                out[n] = res
                done[0] += 1
                print(f"[{done[0]}/{len(targets)}] {n}: {res[1]}, {res[2]} links, {len(res[0])} matching",
                      flush=True)
        await asyncio.gather(*[worker(n, u) for n, u in targets])
        await browser.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated company names to test")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--concurrency", type=int, default=5)
    a = ap.parse_args()

    md = requests.get(v2.README_URL, headers=v2.UA, timeout=v2.TIMEOUT).text
    companies = re.findall(r"-\s+\*\*(.+?)\*\*:\s+\[Career Page\]\((.+?)\)", md)
    cache = v2.load(v2.CACHE_FILE, {})
    targets = [(n, u) for n, u in companies if not cache.get(n, {}).get("ats")]
    if a.only:
        want = {x.strip().lower() for x in a.only.split(",")}
        targets = [(n, u) for n, u in companies if n.lower() in want]
    if a.limit:
        targets = targets[:a.limit]
    if not targets:
        print("Nothing to crawl. Run job_scraper_v2.py first.")
        return
    print(f"Crawling {len(targets)} career pages...")
    try:
        results = asyncio.run(crawl_all(targets, cache, a.concurrency))
    except ImportError:
        sys.exit("pip3 install playwright && python3 -m playwright install chromium")
    json.dump(cache, open(v2.CACHE_FILE, "w"), indent=1)

    seen = set(v2.load(v2.SEEN_FILE, []))
    new, allfound = [], []
    for n, (jobs, status, nlinks) in results.items():
        for j in jobs:
            key = j["url"] if j["url"] and j["url"] != dict(companies).get(n) \
                else f'{n}|{j["title"]}|{j["location"]}'
            j["key"] = key
            allfound.append(j)
            if key not in seen:
                new.append(j)
    exists = os.path.exists("new_jobs.csv")
    with open("new_jobs.csv", "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["company", "title", "location", "posted", "source", "url"])
        for j in new:
            w.writerow([j["company"], j["title"], j["location"],
                        j["posted"].date() if j.get("posted") else "", j["ats"], j["url"]])
    json.dump(sorted(seen | {j["key"] for j in allfound}), open(v2.SEEN_FILE, "w"))
    with open("crawl_report.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["company", "status", "links_seen", "matching_jobs"])
        for n, (jobs, status, nlinks) in sorted(results.items()):
            w.writerow([n, status, nlinks, len(jobs)])
    print(f"Done. {len(allfound)} matching, {len(new)} new appended to new_jobs.csv. "
          f"See crawl_report.csv for sites that failed.")


if __name__ == "__main__":
    main()
