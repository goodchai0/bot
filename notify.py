#!/usr/bin/env python3
"""
notify.py - turns new_jobs.csv into a location-grouped email (and optional Telegram message).

Usage:
    python3 notify.py                          # email everything, grouped by city
    python3 notify.py --cities Bengaluru,Remote  # only these locations
    python3 notify.py --dry-run                # print CSV only: no email, no archive
    python3 notify.py --preview                # write preview.html + jobs.html to open in a browser
    python3 notify.py --keep-all               # do not merge duplicate jobs

Settings come from a .env file in the same folder (see .env.example), or environment variables:
    EMAIL_USER, EMAIL_APP_PASSWORD, EMAIL_TO   (Gmail: use an App Password, not your login password)
    SMTP_HOST (default smtp.gmail.com)
    TG_TOKEN, TG_CHAT                          (optional Telegram)
    FEATURED_COMPANIES                         (optional, comma list added to the built-in star list)
    JOBS_URL                                   (optional, public URL of the hosted jobs page, e.g.
                                                https://<user>.github.io/<repo>/ ; makes the email
                                                "Jump to" chips open the filterable page)
    Other SMTP providers (Brevo etc.): SMTP_HOST, SMTP_USER (login), EMAIL_FROM (verified sender), SMTP_PORT

What you get:
  * Email: a compact, company-grouped digest. It is auto-trimmed to stay under Gmail's ~102 KB
    clipping limit (Gmail hides everything after that - the old "empty section" bug).
    Every job shows where it was found (LinkedIn, Naukri, Indeed, Career site).
  * Attachment jobs.html: open it on your phone/laptop for search + city/company/star filters.
    (Filters need JavaScript: save the file and open it in Chrome/Safari, not an email preview.)
  * docs/index.html: same page, ready to host with GitHub Pages (see JOBS_URL).
  * Plain-text part and Telegram: CSV text. Per-city CSVs + jobs.html saved in history/<date>/.
"""
import argparse, csv, html, json, os, re, shutil, smtplib, sys, time, unicodedata
from collections import Counter, defaultdict
from datetime import date
from email.message import EmailMessage
from urllib.parse import quote

BUCKETS = [
    ("Bengaluru", r"bengaluru|bangalore|\bKA\b|karnataka"),
    ("Hyderabad", r"hyderabad|secunderabad|\bTS\b|telangana"),
    ("Pune", r"\bpune\b"),
    ("Mumbai", r"mumbai|thane"),
    ("Maharashtra", r"\bMH\b|maharashtra"),
    ("Delhi NCR", r"delhi|gurgaon|gurugram|noida|ghaziabad|faridabad"),
    ("Chennai", r"chennai"),
    ("Kolkata", r"kolkata|calcutta"),
    ("Remote", r"remote|work from home|wfh|anywhere"),
]
OTHER = "Other / unspecified"

# Companies you'd be excited about: shown with a star and in the "Top picks" strip.
# Add more without editing code: FEATURED_COMPANIES="Stripe,Databricks" in .env
DEFAULT_FEATURED = [
    "Microsoft", "Google", "Amazon", "Adobe", "Atlassian", "Uber", "Rippling", "Razorpay",
    "Flipkart", "Swiggy", "Zomato", "PhonePe", "Paytm", "Walmart", "Goldman Sachs", "JPMorgan",
    "Morgan Stanley", "American Express", "Barclays", "Citi", "HSBC", "Cisco", "Oracle",
    "Salesforce", "Intuit", "Texas Instruments", "Cohesity", "Mercari", "Motorola Solutions",
    "Elementor", "Maersk", "Cargill", "Johnson Controls", "Societe Generale", "BNY", "Sage",
    "Kulfi", "1% Club", "project44", "Zinier",
]


def bucket(loc):
    for name, pat in BUCKETS:
        if re.search(pat, loc or "", re.I):
            return name
    return OTHER


# ---------------------------------------------------------------- de-duplication
# Same job = same (normalised company, normalised title, city bucket). This merges:
#   * the same posting seen on several sources (linkedin + indeed-direct + naukri ...)
#   * many near-identical requisitions (e.g. 13 x "Accenture - Custom Software Engineer")
#     into ONE row with an "N openings" badge and the experience range found in naukri URLs.
_COMPANY_NOISE = re.compile(
    r"\b(global solutions? cent(?:re|er)|technology services|technologies|technology|"
    r"pvt|private|ltd|limited|inc|llc|llp|corp|corporation|india|in|the)\b")
_CITY_WORDS = re.compile(
    r"\b(bengaluru|bangalore|hyderabad|secunderabad|pune|mumbai|thane|delhi|ncr|gurgaon|"
    r"gurugram|noida|ghaziabad|faridabad|chennai|kolkata|jaipur|kochi|remote|pan|india|"
    r"immediate)\b")
_EXP_RE = re.compile(r"(\d+)-to-(\d+)-years")


def _fold(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def _norm_company(c):
    s = re.sub(r"[^a-z0-9 ]+", " ", _fold(c))
    return " ".join(_COMPANY_NOISE.sub(" ", s).split())


def _norm_title(t, company_norm):
    s = re.sub(r"\([^)]*\)", " ", _fold(t))          # drop "(2-9 y)" style notes
    s = re.sub(r"[^a-z0-9+# ]+", " ", s)
    if company_norm:
        s = s.replace(company_norm, " ")               # "Java Developer -Infosys"
    s = _CITY_WORDS.sub(" ", s)                        # "- Hyderabad", "- PAN India"
    return " ".join(s.split())


def _src_rank(src):
    """Lower = better apply link: employer-site feeds first, then LinkedIn, Naukri, Indeed."""
    s = (src or "").lower()
    if s.endswith("-direct"):
        return 0
    return {"linkedin": 1, "naukri": 2, "indeed": 3}.get(s, 2)


def dedupe(rows):
    groups = {}
    for r in rows:
        co = _norm_company(r.get("company"))
        title = _norm_title(r.get("title"), co) or (r.get("url") or "")
        city = r.get("city") or bucket(r.get("location"))
        groups.setdefault((co, title, city), []).append(r)
    out = []
    for rs in groups.values():
        newest_first = sorted(rs, key=lambda r: r.get("posted") or "", reverse=True)
        best = dict(min(newest_first, key=lambda r: _src_rank(r.get("source"))))
        srcs = sorted({r.get("source") or "" for r in rs} - {""}, key=_src_rank)
        per_source = Counter(r.get("source") for r in rs)   # each source lists a req once
        ranges = [(int(a), int(b)) for r in rs for a, b in _EXP_RE.findall(r.get("url") or "")]
        best["source"] = ", ".join(srcs)
        best["posted"] = newest_first[0].get("posted", "")
        best["openings"] = str(max(per_source.values()))
        best["exp"] = (f"{min(a for a, _ in ranges)}-{max(b for _, b in ranges)} yrs"
                       if ranges else "")
        out.append(best)
    return out


_FEATURED_RE = None


def is_featured(company):
    global _FEATURED_RE
    if _FEATURED_RE is None:
        extra = [x.strip() for x in os.environ.get("FEATURED_COMPANIES", "").split(",") if x.strip()]
        pats = [re.escape(n) for n in (_norm_company(x) for x in DEFAULT_FEATURED + extra) if n]
        _FEATURED_RE = re.compile(r"\b(" + "|".join(pats) + r")\b")
    return bool(_FEATURED_RE.search(_norm_company(company)))


def load_env(path=".env"):
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---------------------------------------------------------------- small helpers
def _safe_url(u):
    u = (u or "").strip()
    return u if re.match(r"https?://", u, re.I) else "#"


def _esc(s):
    return html.escape(s or "", quote=True)


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


_STATE_CODES = {"ka", "ts", "mh", "tn", "dl"}


def _short_loc(loc, city=""):
    """'Bengaluru, Karnataka, India' -> '' (same as its section); 'Bengaluru, Hyderabad, India' stays."""
    s = re.sub(r",?\s*India\s*$", "", loc or "", flags=re.I)
    s = re.sub(r"\bKarnataka\b,?", "", s, flags=re.I).strip(" ,")
    if not s or s.lower() in _STATE_CODES or s.lower() == (city or "").lower():
        return ""
    return s if len(s) <= 40 else s[:37] + "..."


# Human-friendly source names. "<anything>-direct" feeds are employer career sites.
_SRC_NAMES = {"linkedin": "LinkedIn", "naukri": "Naukri", "indeed": "Indeed"}


def _src_label(s):
    s = (s or "").strip()
    if s.lower().endswith("-direct"):
        return "Career site"
    return _SRC_NAMES.get(s.lower(), s.title())


def _src_labels(srcs):
    """'linkedin-direct, linkedin, naukri' -> 'Career site, LinkedIn, Naukri' (first = apply link)."""
    seen = []
    for s in (srcs or "").split(","):
        if not s.strip():
            continue
        lab = _src_label(s)
        if lab not in seen:
            seen.append(lab)
    return ", ".join(seen)


def _group_companies(rows):
    d = {}
    for r in rows:
        d.setdefault(_norm_company(r.get("company")), []).append(r)
    return [(rs[0].get("company"), rs) for rs in d.values()]


_AVATAR = ["#5b63c9", "#3f8fa3", "#4a9a7c", "#b9853a", "#b85c5c", "#8a6fc4", "#b8618f", "#69758a"]


def _avatar(name):
    ch = (re.sub(r"[^A-Za-z0-9]", "", name or "") or "?")[0].upper()
    col = _AVATAR[sum(map(ord, name or "")) % len(_AVATAR)]
    return ('<b style="background:%s;color:#fff;border-radius:5px;padding:2px 7px;'
            'font-size:12px;margin-right:8px">%s</b>' % (col, _esc(ch)))


# ---------------------------------------------------------------- email (compact digest)
def _render_email(groups, total, raw_total, max_roles):
    today = f"{date.today():%A, %d %b %Y}"
    all_rows = [r for _, rs in groups for r in rs]
    n_comp = len({_norm_company(r.get("company")) for r in all_rows})
    dup = ""
    if raw_total and raw_total > total:
        dup = f" &middot; {raw_total - total} duplicates merged"

    # Gmail strips id/name anchors, so in-email "#c-..." links go nowhere. When JOBS_URL is set,
    # the chips open the hosted, filterable page already filtered to that city instead.
    jobs_url = (os.environ.get("JOBS_URL") or "").strip()

    def _nav_href(c):
        return f"{jobs_url}#city={quote(c, safe='')}" if jobs_url else "#c-" + _slug(c)

    nav = "".join(
        '<a href="%s" style="display:inline-block;margin:0 6px 6px 0;padding:5px 11px;'
        'background-color:#12233f;color:#79c0ff;border-radius:999px;font-size:12px;font-weight:600;'
        'text-decoration:none;white-space:nowrap">%s &middot; %d</a>'
        % (_esc(_nav_href(c)), _esc(c), len(rs))
        for c, rs in groups)

    feat, names = Counter(), {}
    for r in all_rows:
        if r.get("featured"):
            k = _norm_company(r.get("company"))
            feat[k] += 1
            names.setdefault(k, r.get("company"))
    picks = ""
    if feat:
        chips = []
        for k, n in feat.most_common():
            cnt = f" &middot; {n}" if n > 1 else ""
            chips.append('<span style="display:inline-block;margin:0 6px 6px 0;padding:5px 10px;'
                         'background-color:#3d2e00;color:#e3b341;border-radius:999px;font-size:12px;'
                         'font-weight:700;white-space:nowrap">&#11088; %s%s</span>' % (_esc(names[k]), cnt))
        picks = ('<div style="margin-top:14px;font-size:12px;font-weight:700;color:#e3b341;'
                 'text-transform:uppercase;letter-spacing:.6px">Top picks hiring now</div>'
                 '<div style="margin-top:6px">%s</div>' % "".join(chips))

    body, hidden = [], 0
    for city, rs in groups:
        slug = _slug(city)
        comps = _group_companies(rs)
        body.append(
            '<tr><td id="c-%s" style="padding:22px 0 8px;border-bottom:2px solid #388bfd">'
            '<a name="c-%s"></a><span style="font-size:18px;font-weight:700;color:#e6edf3">%s</span>'
            '<span style="font-size:12px;color:#8b949e">&nbsp; %d job%s &middot; %d compan%s</span>'
            '&nbsp; <a href="#top" style="font-size:12px;color:#58a6ff;text-decoration:none">&uarr; top</a>'
            '</td></tr>' % (slug, slug, _esc(city), len(rs), "s" if len(rs) > 1 else "",
                         len(comps), "ies" if len(comps) > 1 else "y"))
        for name, crs in comps:
            star = " &#11088;" if crs[0].get("featured") else ""
            cnt = (' <span style="font-size:11px;color:#8b949e;font-weight:600">&middot; %d roles</span>'
                   % len(crs)) if len(crs) > 1 else ""
            shown = crs if max_roles is None else crs[:max_roles]
            hidden += len(crs) - len(shown)
            head = _esc(name)
            if max_roles == 0:
                head = '<a href="%s" style="color:#e6edf3;text-decoration:none">%s</a>' % (
                    _esc(_safe_url(crs[0].get("url"))), head)
            lines = []
            for r in shown:
                meta = []
                loc = _short_loc(r.get("location"), city)
                if loc:
                    meta.append(_esc(loc))
                if r.get("exp"):
                    meta.append("&#128188; " + _esc(r["exp"]))
                n = int(r.get("openings") or 1)
                if n > 1:
                    meta.append(f"{n} openings")
                srcl = _src_labels(r.get("source"))
                if srcl:
                    meta.append("via " + _esc(srcl))
                sub = ('<br><span style="font-size:12px;color:#8b949e">%s</span>'
                       % " &middot; ".join(meta)) if meta else ""
                lines.append(
                    '<div style="padding:6px 0 0 32px">'
                    '<a href="%s" style="color:#58a6ff;text-decoration:none;font-weight:600">%s</a>%s</div>'
                    % (_esc(_safe_url(r.get("url"))), _esc(r.get("title")), sub))
            more = ""
            if max_roles and len(crs) > len(shown):
                more = ('<div style="padding:4px 0 0 32px;font-size:12px;color:#8b949e">'
                        '+%d more roles &middot; see attached jobs.html</div>' % (len(crs) - len(shown)))
            body.append('<tr><td style="padding:0 0 10px;font-size:14px;line-height:1.45">'
                        '<div style="background-color:#161b22;border-radius:12px;'
                        'padding:13px 14px">%s'
                        '<b style="font-size:15px;color:#e6edf3">%s</b>%s%s%s%s</div></td></tr>'
                        % (_avatar(name), head, star, cnt, "".join(lines), more))

    if hidden:
        foot = (f"Trimmed to keep this email under Gmail's size limit. Open the attached "
                f"<b>jobs.html</b> for all {total} roles with search and filters.")
    else:
        foot = "Open the attached <b>jobs.html</b> to search and filter by city, company or star."
    if jobs_url:
        foot += f' <a href="{_esc(jobs_url)}" style="color:#58a6ff">Open filterable page</a>'

    font = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='color-scheme' content='dark'><meta name='supported-color-schemes' content='dark'>"
        f"<title>{total} new jobs</title></head>"
        f"<body bgcolor=\"#0d1117\" style=\"margin:0;padding:0;background-color:#0d1117;font-family:{font}\">"
        f"<div style='display:none;max-height:0;overflow:hidden;opacity:0'>"
        f"{total} jobs at {n_comp} companies across {len(groups)} locations</div>"
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' "
        "bgcolor='#0d1117' style='background-color:#0d1117'><tr><td align='center' style='padding:12px 8px'>"
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='max-width:640px'>"
        "<tr><td id='top' bgcolor='#161b22' style='background-color:#161b22;border-bottom:1px solid #30363d;border-radius:12px 12px 0 0;padding:22px 20px'>"
        "<a name='top'></a>"
        f"<div style='font-size:24px;font-weight:700;color:#f0f6fc'>{total} new jobs</div>"
        f"<div style='font-size:13px;color:#8b949e;margin-top:4px'>{n_comp} companies &middot; "
        f"{len(groups)} locations</div>"
        f"<div style='font-size:12px;color:#6e7681;margin-top:2px'>{_esc(today)}{dup}</div>"
        "</td></tr>"
        "<tr><td bgcolor='#0d1117' style='background-color:#0d1117;padding:16px 20px 6px'>"
        "<div style='font-size:12px;font-weight:700;color:#79c0ff;text-transform:uppercase;"
        "letter-spacing:.6px;margin-bottom:6px'>Jump to</div>"
        f"{nav}{picks}</td></tr>"
        "<tr><td bgcolor='#0d1117' style='background-color:#0d1117;padding:0 20px 16px;border-radius:0 0 12px 12px'>"
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0'>"
        + "".join(body) +
        "</table></td></tr>"
        f"<tr><td align='center' style='padding:14px 8px;font-size:12px;color:#8b949e'>{foot}</td></tr>"
        "</table></td></tr></table></body></html>"
    ), hidden


def build_html(groups, total, raw_total=None, limit=97_000):
    """Render the email; if it would be too big for Gmail (clips at ~102 KB), show fewer roles
    per company until it fits (nothing is lost: jobs.html has everything)."""
    out = ""
    for max_roles in (None, 5, 3, 2, 1, 0):
        out, _ = _render_email(groups, total, raw_total, max_roles)
        if len(out.encode("utf-8")) <= limit:
            break
    return out


# ---------------------------------------------------------------- interactive page (attachment)
_PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f3f4f6;--card:#fff;--tx:#111827;--mu:#6b7280;--bd:#e5e7eb;--ac:#4f46e5;--ln:#1d4ed8;--chip:#eef2ff;--chipx:#3730a3;--star:#fef3c7;--starx:#92400e}
@media(prefers-color-scheme:dark){:root{--bg:#0d1117;--card:#161b22;--tx:#e6edf3;--mu:#8b949e;--bd:#30363d;--ac:#388bfd;--ln:#58a6ff;--chip:#12233f;--chipx:#79c0ff;--star:#3d2e00;--starx:#e3b341}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;padding-top:env(safe-area-inset-top,0)}
header{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--bd);padding:12px 12px 8px}
.wrap{max-width:720px;margin:0 auto}
h1{font-size:19px;margin:0}.sub{color:var(--mu);font-size:12px;margin:2px 0 10px}
.row{display:flex;gap:8px;align-items:center}
input[type=search]{flex:1;min-width:0;padding:10px 12px;border:1px solid var(--bd);border-radius:10px;background:var(--bg);color:var(--tx);font-size:16px}
.strip{display:flex;gap:6px;overflow-x:auto;padding:8px 0 2px;-webkit-overflow-scrolling:touch;scrollbar-width:none}
.strip::-webkit-scrollbar{display:none}
.chip{flex:none;border:1px solid transparent;background:var(--chip);color:var(--chipx);border-radius:999px;padding:6px 12px;font:600 13px inherit;font-family:inherit;cursor:pointer;white-space:nowrap}
.chip b{opacity:.7;font-weight:600}
.chip.star{background:var(--star);color:var(--starx)}
.chip.on{background:var(--ac);color:#fff}
.chip.on b{opacity:.9}
.lbl{font-size:11px;font-weight:700;color:var(--mu);text-transform:uppercase;letter-spacing:.6px;margin-top:8px}
.tog{flex:none}
main{max-width:720px;margin:0 auto;padding:4px 12px 40px}
h2{font-size:18px;margin:22px 0 8px;padding-bottom:6px;border-bottom:2px solid var(--ac)}
h2 span{font-size:12px;color:var(--mu);font-weight:500;margin-left:6px}
.co{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:12px 14px;margin:0 0 10px}
.ch{display:flex;align-items:center;gap:8px;font-size:15px}
.ch i{width:24px;height:24px;border-radius:7px;color:#fff;font:700 12px/24px sans-serif;text-align:center;font-style:normal;flex:none}
.mu{color:var(--mu);font-size:12px;font-weight:600}
.role{padding:8px 0 0 32px}
.role a{color:var(--ln);font-weight:600;text-decoration:none}
.meta{font-size:12px;color:var(--mu);margin-top:2px}
.pill{background:var(--star);color:var(--starx);border-radius:999px;padding:1px 7px;font-weight:600}
.empty{text-align:center;color:var(--mu);padding:40px 0}
#count{font-size:12px;color:var(--mu);margin-top:6px}
button.link{background:none;border:0;color:var(--ac);font:600 12px inherit;font-family:inherit;cursor:pointer;padding:0 0 0 8px}
</style></head><body>
<header><div class="wrap">
<h1>__TITLE__</h1><div class="sub">__META__</div>
<div class="row"><input id="q" type="search" placeholder="Search company, role or location" autocomplete="off">
<button class="chip tog star" id="feat">&#11088; Top picks</button></div>
<div class="lbl">Location</div><div class="strip" id="cities"></div>
<div class="lbl">Companies</div><div class="strip" id="cos"></div>
<div id="count"></div>
</div></header>
<main id="list"></main>
<noscript><p class="empty">Filters need JavaScript. Save this file and open it in Chrome or Safari.</p></noscript>
<script>
const J=__DATA__,CITIES=__CITIES__;
const st={q:"",city:"",co:"",feat:false};
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const safe=u=>/^https?:\/\//i.test(u)?u:"#";
const PAL=["#4f46e5","#0891b2","#059669","#d97706","#dc2626","#7c3aed","#db2777","#475569"];
const color=s=>{let h=0;for(const ch of s)h+=ch.charCodeAt(0);return PAL[h%PAL.length]};
function match(j,skipCo){
  if(st.city&&j.city!==st.city)return false;
  if(st.feat&&!j.f)return false;
  if(!skipCo&&st.co&&j.ck!==st.co)return false;
  if(st.q){const h=(j.c+" "+j.t+" "+j.l).toLowerCase();return st.q.split(/\s+/).every(w=>h.includes(w))}
  return true;
}
function chips(){
  $("#cities").innerHTML=[["",J.length]].concat(CITIES).map(a=>`<button class="chip${st.city===a[0]?" on":""}" data-city="${esc(a[0])}">${a[0]?esc(a[0]):"All"} <b>${a[1]}</b></button>`).join("");
  const m=new Map();
  J.filter(j=>match(j,true)).forEach(j=>{const o=m.get(j.ck)||{n:j.c,k:j.ck,c:0,f:j.f};o.c++;m.set(j.ck,o)});
  const top=[...m.values()].sort((a,b)=>(b.f-a.f)||(b.c-a.c)||a.n.localeCompare(b.n));
  $("#cos").innerHTML=top.map(o=>`<button class="chip${o.f?" star":""}${st.co===o.k?" on":""}" data-co="${esc(o.k)}">${o.f?"&#11088; ":""}${esc(o.n)} <b>${o.c}</b></button>`).join("");
  $("#feat").classList.toggle("on",st.feat);
}
function list(){
  const rows=J.filter(j=>match(j,false));
  const filt=st.q||st.city||st.co||st.feat;
  $("#count").innerHTML=rows.length+" of "+J.length+" jobs"+(filt?' <button class="link" id="clear">Clear filters</button>':"");
  if(!rows.length){$("#list").innerHTML='<p class="empty">No jobs match. Try clearing a filter.</p>';return}
  const byCity=new Map();
  rows.forEach(j=>{if(!byCity.has(j.city))byCity.set(j.city,new Map());const m=byCity.get(j.city);if(!m.has(j.ck))m.set(j.ck,[]);m.get(j.ck).push(j)});
  let h="";
  byCity.forEach((cos,city)=>{
    let n=0;cos.forEach(a=>n+=a.length);
    h+=`<h2>${esc(city)}<span>${n} job${n>1?"s":""} &middot; ${cos.size} compan${cos.size>1?"ies":"y"}</span></h2>`;
    cos.forEach(a=>{
      const f=a[0];
      h+=`<section class="co"><div class="ch"><i style="background:${color(f.c)}">${esc((f.c.match(/[A-Za-z0-9]/)||["?"])[0].toUpperCase())}</i><b>${esc(f.c)}</b>${f.f?" &#11088;":""}${a.length>1?`<span class="mu">&middot; ${a.length} roles</span>`:""}</div>`;
      a.forEach(j=>{
        const meta=[];
        if(j.l)meta.push("&#128205; "+esc(j.l));
        if(j.e)meta.push("&#128188; "+esc(j.e));
        if(j.p)meta.push("&#128337; "+esc(j.p));
        if(j.n>1)meta.push('<span class="pill">'+j.n+' openings</span>');
        if(j.s)meta.push("via "+esc(j.s));
        h+=`<div class="role"><a href="${esc(safe(j.u))}" target="_blank" rel="noopener">${esc(j.t)}</a><div class="meta">${meta.join(" &middot; ")}</div></div>`;
      });
      h+="</section>";
    });
  });
  $("#list").innerHTML=h;
}
function render(){chips();list()}
document.addEventListener("click",e=>{
  const b=e.target.closest("button");if(!b)return;
  if(b.dataset.city!==undefined){st.city=b.dataset.city;st.co=""}
  else if(b.dataset.co!==undefined){st.co=st.co===b.dataset.co?"":b.dataset.co}
  else if(b.id==="feat"){st.feat=!st.feat}
  else if(b.id==="clear"){st.q="";st.city="";st.co="";st.feat=false;$("#q").value=""}
  else return;
  render();window.scrollTo({top:0});
});
$("#q").addEventListener("input",e=>{st.q=e.target.value.trim().toLowerCase();render()});
/* deep links from the email: page.html#city=Pune&feat=1&q=java */
try{const p=new URLSearchParams(location.hash.slice(1));
if(p.get("city"))st.city=p.get("city");
if(p.get("feat"))st.feat=true;
if(p.get("q")){st.q=p.get("q").toLowerCase();$("#q").value=p.get("q")}}catch(e){}
render();
</script></body></html>
"""


def build_page(groups, total, raw_total=None):
    data = []
    for city, rs in groups:
        for r in rs:
            data.append({
                "c": r.get("company", ""), "ck": _norm_company(r.get("company")),
                "t": r.get("title", ""), "l": _short_loc(r.get("location"), city), "city": city,
                "p": r.get("posted", ""), "s": _src_labels(r.get("source")),
                "u": _safe_url(r.get("url")),
                "n": int(r.get("openings") or 1), "e": r.get("exp", ""),
                "f": bool(r.get("featured")),
            })
    n_comp = len({d["ck"] for d in data})
    meta = f"{n_comp} companies - {len(groups)} locations - {date.today():%d %b %Y}"
    if raw_total and raw_total > total:
        meta += f" - {raw_total - total} duplicates merged"
    payload = lambda o: json.dumps(o, ensure_ascii=False).replace("</", "<\\/")
    cities = [[c, len(rs)] for c, rs in groups]
    return (_PAGE.replace("__TITLE__", html.escape(f"{total} new jobs"))
            .replace("__META__", html.escape(meta))
            .replace("__DATA__", payload(data))
            .replace("__CITIES__", payload(cities)))


# ---------------------------------------------------------------- csv / markdown / sending
CSV_FIELDS = ["company", "title", "location", "posted", "source", "url", "openings", "exp"]


def build_text(groups, total):
    """All jobs as CSV text: company,title,location,posted,source,url,openings,exp."""
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
            n = int(r.get("openings") or 1)
            more = f" (x{n} openings)" if n > 1 else ""
            srcl = _src_labels(r.get("source"))
            via = f" - via {srcl}" if srcl else ""
            out.append(f"- **{r['company']}** - [{r['title']}]({r['url']}){more} - {r['location']}{posted}{via}")
        out.append("")
    return "\n".join(out)


def send_email(subject, text, html_body=None, attachment=None):
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
    # Everyone in EMAIL_TO is BCC'd: the visible "To" is just the sender, and the real recipients
    # are passed only to the SMTP envelope (to_addrs below), so nobody sees anyone else's address.
    msg["Subject"], msg["From"], msg["To"] = subject, from_addr, from_addr
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    if attachment:
        fname, data = attachment
        msg.add_attachment(data, maintype="text", subtype="html", filename=fname)

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
    ap.add_argument("--keep-all", action="store_true", help="do not merge duplicate jobs")
    ap.add_argument("--preview", action="store_true",
                    help="write preview.html and jobs.html to open in a browser; sends nothing")
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
    raw_total = len(rows)
    if not a.keep_all:
        rows = dedupe(rows)
        print(f"Dedupe: {raw_total} listings -> {len(rows)} unique jobs")
    for r in rows:
        r["featured"] = is_featured(r.get("company"))

    by_city = defaultdict(list)
    for r in rows:
        by_city[r["city"]].append(r)
    order = [n for n, _ in BUCKETS] + [OTHER]
    groups = [(c, sorted(by_city[c], key=lambda r: (not r["featured"], r["company"].lower(),
                                                    r["title"].lower())))
              for c in order if c in by_city]
    total = len(rows)

    text = build_text(groups, total)
    if a.preview:
        with open("preview.html", "w", encoding="utf-8") as f:
            f.write(build_html(groups, total, raw_total))
        with open("jobs.html", "w", encoding="utf-8") as f:
            f.write(build_page(groups, total, raw_total))
        print("Wrote preview.html (the email) and jobs.html (the filterable page) - open in a browser.")
        return
    if a.dry_run:
        print(text)
        return

    with open("message.md", "w", encoding="utf-8") as f:     # used by the GitHub Issue step
        f.write(build_markdown(groups, total))

    outdir = os.path.join("history", date.today().isoformat())
    os.makedirs(outdir, exist_ok=True)
    page = build_page(groups, total, raw_total)
    with open(os.path.join(outdir, "jobs.html"), "w", encoding="utf-8") as f:
        f.write(page)
    # Hosted copy for GitHub Pages (Settings -> Pages -> branch main, folder /docs).
    os.makedirs("docs", exist_ok=True)
    with open(os.path.join("docs", "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    for city, rs in groups:
        fn = os.path.join(outdir, re.sub(r"[^A-Za-z0-9]+", "_", city) + ".csv")
        with open(fn, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rs)
    shutil.copy(a.csv, os.path.join("history", f"new_jobs_{date.today().isoformat()}.csv"))

    email_ok = True
    try:
        counts = ", ".join(f"{c} {len(r)}" for c, r in groups)
        email_ok = send_email(f"{total} new jobs - {date.today():%d %b} ({counts})",
                              text, build_html(groups, total, raw_total),
                              (f"jobs-{date.today():%d-%b}.html", page.encode("utf-8")))
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
    print(f"Per-city CSVs and jobs.html saved in {outdir}/")
    if not email_ok:
        sys.exit("DELIVERY FAILED on every configured channel - exiting with error so this run's "
                 "state is not saved and these jobs are re-sent next time.")


if __name__ == "__main__":
    main()
