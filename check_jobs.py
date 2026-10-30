"""Erin's job alerts: checks big tech + quant career sites for new
data science / ML / data engineering / research / quant internships
and sends a push notification through ntfy.

Run every 10 minutes by GitHub Actions (.github/workflows/jobs.yml).
Set DRY_RUN=1 to print matches without notifying or saving.
"""
import json, os, re, sys, time, pathlib, urllib.request, urllib.parse, http.cookiejar
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).parent
SEEN_FILE = ROOT / "seen.json"
STATUS_FILE = ROOT / "status.json"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept": "application/json, text/xml, */*"}

# ---------------- what counts as a match ----------------
INTERN_RE = re.compile(r"\bintern(s|ship|ships)?\b|\bco-?op\b|student researcher|\bapprentice", re.I)
FIELD_RE = re.compile(
    r"data scien|data scientist|machine learning|\bml\b|\bai\b|\bai/ml\b|artificial intelligence|"
    r"applied scien|research|\bnlp\b|natural language|deep learning|computer vision|\bllm|"
    r"data engineer|data & ai|data and ai|data platform|analytics engineer|"
    r"quant|quantitative|trading|trader|algorithmic", re.I)
GRAD_ONLY_RE = re.compile(r"\bph\.?\s?d\b|doctoral|\bmba\b|\bmasters?\b|master['’]s|\bm\.?s\.?\b|postdoc|"
                          r"graduate intern|\bm1\b|\bm2\b", re.I)
EXCLUDE_RE = re.compile(r"program manager|ux research|user research|experience research|marketing|"
                        r"trading card|collectible|security research|recruit", re.I)
UNDERGRAD_RE = re.compile(r"\bb\.?s\.?\b|bachelor|undergrad|\bba\b|\bbs/ms\b", re.I)

def title_matches(title):
    if not INTERN_RE.search(title) or not FIELD_RE.search(title):
        return False
    # skip roles that are explicitly PhD / Master's / MBA only
    if GRAD_ONLY_RE.search(title) and not UNDERGRAD_RE.search(title):
        return False
    if EXCLUDE_RE.search(title):
        return False
    # skip past terms (e.g. "Fall 2026") but keep anything mentioning 2027
    if "2026" in title and "2027" not in title and "winter" not in title.lower():
        return False
    return True

# ---------------- where she can work ----------------
# Abroad is allowed only where she can get a work visa without employer sponsorship:
#   South Korea (F-4 overseas Korean visa), Singapore (Work Holiday Pass),
#   Australia and New Zealand (working holiday visas), Ireland (working holiday for US students).
# Canada (IEC through a sponsor organization) and the UK (GAE sponsor) need a third party, so they're off.
ALLOWED_ABROAD_RE = re.compile(r"korea|seoul|busan|pangyo|singapore|australia|sydney|melbourne|brisbane|"
                               r"new zealand|auckland|wellington|ireland|dublin|cork", re.I)
FOREIGN_RE = re.compile(
    r"\b(uk|u\.k\.|gbr|united kingdom|england|scotland|wales|london|manchester|warwick|edinburgh|guildford|"
    r"canada|toronto|vancouver|montreal|montréal|ottawa|waterloo|calgary|\bbc\b|ontario|quebec|qc|"
    r"germany|berlin|munich|münchen|hamburg|france|paris|grenoble|lyon|switzerland|zurich|zürich|lausanne|geneva|"
    r"netherlands|amsterdam|eindhoven|belgium|brussels|luxembourg|spain|madrid|barcelona|portugal|lisbon|"
    r"italy|milan|rome|austria|vienna|denmark|copenhagen|sweden|stockholm|norway|oslo|finland|helsinki|"
    r"poland|gdansk|gdańsk|warsaw|krakow|kraków|czech|prague|hungary|budapest|romania|bucharest|iasi|"
    r"israel|tel aviv|haifa|india|bangalore|bengaluru|hyderabad|pune|chennai|gurgaon|noida|mumbai|delhi|"
    r"china|prc|shanghai|beijing|shenzhen|hangzhou|taiwan|taipei|hsinchu|hong kong|japan|tokyo|osaka|"
    r"vietnam|hanoi|ho chi minh|malaysia|kuala lumpur|penang|indonesia|jakarta|thailand|bangkok|philippines|manila|"
    r"uae|dubai|abu dhabi|saudi|riyadh|qatar|doha|egypt|cairo|turkey|istanbul|south africa|cape town|"
    r"nigeria|lagos|kenya|nairobi|ghana|accra|mexico|guadalajara|brazil|são paulo|sao paulo|argentina|"
    r"buenos aires|colombia|bogotá|bogota|chile|santiago|peru|lima|costa rica)\b", re.I)
US_RE = re.compile(r"united states|\busa?\b|remote|virtual|\b(al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|"
                   r"ky|la|me|md|ma|mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|"
                   r"wa|wv|wi|wy|dc)\b|nyc|\bsf\b|california|new york|texas|washington|seattle|chicago|boston|"
                   r"austin|atlanta|denver|san jose|mountain view|palo alto|sunnyvale|menlo park|redmond|bellevue", re.I)

def location_ok(location):
    """True if at least one of the posting's locations is in the US or an allowed country."""
    loc = (location or "").strip()
    if not loc or re.fullmatch(r"\d+ locations", loc, re.I):
        return True                                  # unknown or "3 Locations": keep, can't tell
    parts = [x.strip() for x in re.split(r";|•|\||/| and ", loc) if x.strip()]
    for part in parts:
        if part.upper() == "IN":
            continue                                 # Microsoft's code for India
        if ALLOWED_ABROAD_RE.search(part):
            return True
        if FOREIGN_RE.search(part):
            continue                                 # clearly abroad, check the next location
        return True                                  # US or unrecognized: keep
    return False

# ---------------- http helpers ----------------
def fetch(url, data=None, headers=None, opener=None, timeout=30):
    h = dict(UA); h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode(); h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=h)
    with (opener.open(req, timeout=timeout) if opener else urllib.request.urlopen(req, timeout=timeout)) as r:
        return r.read().decode("utf-8", "replace"), r.headers

def fetch_json(*a, **k):
    return json.loads(fetch(*a, **k)[0])

def job(company, title, url, location="", jid=None):
    return {"company": company, "title": " ".join(title.split()), "url": url,
            "location": location or "", "id": f"{company}:{jid or url}"}

# ---------------- sources ----------------
WORKDAY = {  # company: (host, tenant, site); host "myworkdaysite" means wd1.myworkdaysite.com
    "Salesforce": ("salesforce.wd12", "salesforce", "External_Career_Site"),
    "Cisco": ("cisco.wd5", "cisco", "Cisco_Careers"),
    "NVIDIA": ("nvidia.wd5", "nvidia", "NVIDIAExternalCareerSite"),
    "Adobe": ("adobe.wd5", "adobe", "external_experienced"),
    "Intel": ("intel.wd1", "intel", "External"),
    "Capital One": ("capitalone.wd12", "capitalone", "Capital_One"),
    "PayPal": ("paypal.wd1", "paypal", "jobs"),
    "Mastercard": ("mastercard.wd1", "mastercard", "CorporateCareers"),
    "Workday": ("workday.wd5", "workday", "Workday"),
    "Disney": ("disney.wd5", "disney", "disneycareer"),
    "Morgan Stanley": ("ms.wd5", "ms", "External"),
    "Bank of America": ("ghr.wd1", "ghr", "Lateral-US"),
    "Wells Fargo": ("myworkdaysite", "wf", "WellsFargoJobs"),
    "Fidelity": ("fmr.wd1", "fmr", "FidelityCareers"),
    "Target": ("target.wd5", "target", "targetcareers"),
    "Nike": ("nike.wd1", "nike", "nke"),
    "T-Mobile": ("tmobile.wd1", "tmobile", "External"),
    "Verizon": ("verizon.wd12", "verizon", "verizon-careers"),
    "Snap": ("myworkdaysite", "snapchat", "snap"),
    "Autodesk": ("autodesk.wd1", "autodesk", "Ext"),
    "CVS Health": ("cvshealth.wd1", "cvshealth", "CVS_Health_Careers"),
    "Humana": ("humana.wd5", "humana", "Humana_External_Career_Site"),
    "Zillow": ("zillow.wd5", "zillow", "Zillow_Group_External"),
    "Allstate": ("allstate.wd5", "allstate", "allstate_careers"),
    "HPE": ("hpe.wd5", "hpe", "Jobsathpe"),
    "Visa": ("visa.wd5", "visa", "Visa"),
}
GREENHOUSE = {"Airbnb": "airbnb", "Stripe": "stripe", "Pinterest": "pinterest",
              "Databricks": "databricks", "Lyft": "lyft", "Figma": "figma", "Scale AI": "scaleai"}
ASHBY = {"OpenAI": "openai", "Snowflake": "snowflake"}
# big tech without a usable public feed, plus quant firms: covered through
# SimplifyJobs' community-maintained internship list (slower, usually hours)
SIMPLIFY_BIG_TECH = {"meta", "uber", "linkedin", "tesla", "bloomberg", "spotify", "ibm", "oracle"}
# well-known quant firms only (small shops left out on purpose)
BIG_QUANT_RE = re.compile(r"jane street|citadel|two sigma|hudson river|jump trading|d\.?\s?e\.? shaw|"
                          r"\bimc\b|optiver|susquehanna|\bsig\b|\bdrw\b|akuna|virtu|millennium|"
                          r"point72|five rings|tower research", re.I)

def src_workday():
    out, errors = [], []
    for company, (host, tenant, site) in WORKDAY.items():
        if host == "myworkdaysite":
            api = f"https://wd1.myworkdaysite.com/wday/cxs/{tenant}/{site}/jobs"
            public = f"https://wd1.myworkdaysite.com/en-US/recruiting/{tenant}/{site}"
        else:
            api = f"https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
            public = f"https://{host}.myworkdayjobs.com/en-US/{site}"
        try:
            for offset in (0, 20, 40):
                # Workday returns newest first; checking the 60 newest every 10 minutes is plenty
                d = fetch_json(api, {"appliedFacets": {}, "limit": 20, "offset": offset,
                                     "searchText": "intern internship"})
                for p in d.get("jobPostings") or []:
                    path = p.get("externalPath")
                    if not path or not p.get("title"):
                        continue
                    out.append(job(company, p["title"], public + path, p.get("locationsText", ""), path))
                time.sleep(0.3)
        except Exception as e:  # one broken company never stops the rest
            errors.append(f"{company}: {type(e).__name__}")
    if errors:
        print("Workday errors:", errors)
    return out

def src_greenhouse():
    out = []
    for company, board in GREENHOUSE.items():
        for p in fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs")["jobs"]:
            out.append(job(company, p["title"], p["absolute_url"], (p.get("location") or {}).get("name", ""), p["id"]))
    return out

def src_ashby():
    out = []
    for company, board in ASHBY.items():
        for p in fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}")["jobs"]:
            out.append(job(company, p["title"], p.get("jobUrl", ""), p.get("location", ""), p.get("id")))
    return out

def src_amazon():
    d = fetch_json("https://www.amazon.jobs/en/search.json?base_query=intern&result_limit=100&sort=recent")
    return [job("Amazon", p["title"], "https://www.amazon.jobs" + p["job_path"],
                p.get("normalized_location") or p.get("location", ""), p.get("id_icims") or p["job_path"])
            for p in d.get("jobs", [])]

def src_microsoft():
    d = fetch_json("https://apply.careers.microsoft.com/api/pcsx/search?domain=microsoft.com"
                   "&query=intern&start=0&sort_by=timestamp")
    return [job("Microsoft", p["name"], f"https://apply.careers.microsoft.com/careers/job/{p['id']}",
                "; ".join(p.get("standardizedLocations") or p.get("locations") or []), p["id"])
            for p in d["data"]["positions"]]

def src_netflix():
    d = fetch_json("https://explore.jobs.netflix.net/api/apply/v2/jobs?domain=netflix.com"
                   "&query=intern&num=50&sort_by=timestamp")
    return [job("Netflix", p["name"], p.get("canonicalPositionUrl", ""), p.get("location", ""), p["id"])
            for p in d.get("positions", [])]

def src_apple():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    _, hdrs = fetch("https://jobs.apple.com/api/v1/CSRFToken", opener=op)
    h = {"X-Apple-CSRF-Token": hdrs.get("X-Apple-CSRF-Token", ""), "Origin": "https://jobs.apple.com",
         "Referer": "https://jobs.apple.com/en-us/search"}
    out = []
    for page in (1, 2, 3):
        d = fetch_json("https://jobs.apple.com/api/v1/search",
                       {"query": "intern", "filters": {}, "page": page, "locale": "en-us", "sort": "newest",
                        "format": {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"}},
                       headers=h, opener=op)
        res = d.get("res", d)
        for p in res.get("searchResults", []):
            loc = "; ".join(l.get("name", "") for l in (p.get("locations") or []) if isinstance(l, dict))
            out.append(job("Apple", p.get("postingTitle", ""),
                           f"https://jobs.apple.com/en-us/details/{p.get('positionId')}/{p.get('transformedPostingTitle', '')}",
                           loc, p.get("positionId")))
    return out

def src_google():
    xml_text, _ = fetch("https://www.google.com/about/careers/applications/jobs/feed.xml", timeout=60)
    root = ET.fromstring(xml_text)
    out = []
    for j in root.iter("job"):
        title = (j.findtext("title") or "").strip()
        if not INTERN_RE.search(title):
            continue
        jid = j.findtext("jobid") or ""
        url = j.findtext("url") or f"https://www.google.com/about/careers/applications/jobs/results/{jid}"
        locs = [l.findtext("city") or l.text or "" for l in j.iter("location")]
        out.append(job("Google", title, url, "; ".join(x.strip() for x in locs if x and x.strip())[:120], jid))
    return out

def src_simplify():
    d = fetch_json("https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
                   timeout=60)
    out = []
    for p in d:
        if not p.get("active") or not p.get("is_visible", True):
            continue
        company = p.get("company_name", "")
        if company.strip().lower() not in SIMPLIFY_BIG_TECH and not BIG_QUANT_RE.search(company):
            continue
        degrees = p.get("degrees") or []
        if degrees and not any("bachelor" in x.lower() for x in degrees):
            continue  # PhD / Master's only
        title = p.get("title", "")
        if not INTERN_RE.search(title):
            title = title + " (Intern)"  # everything on this list is an internship
        out.append(job(company, title, p.get("url", ""), "; ".join(p.get("locations") or []), p.get("id")))
    return out

SOURCES = {"Workday": src_workday, "Greenhouse": src_greenhouse, "Ashby": src_ashby,
           "Amazon": src_amazon, "Microsoft": src_microsoft, "Netflix": src_netflix,
           "Apple": src_apple, "Google": src_google, "Simplify list (Meta, quant, etc.)": src_simplify}

DIRECT_COMPANIES = {c.lower() for c in [*WORKDAY, *GREENHOUSE, *ASHBY,
                                        "Amazon", "Microsoft", "Netflix", "Apple", "Google"]}

# ---------------- notify ----------------
def notify(title, message, click=None, tags="briefcase"):
    if DRY_RUN or not NTFY_TOPIC:
        print("NOTIFY:", title, "|", message.replace("\n", " | "))
        return
    # "high" priority = long vibration and iOS time-sensitive alert, so it pops up as a banner
    h = {"Title": title.encode("ascii", "ignore").decode() or "Job alert", "Tags": tags, "Priority": "high"}
    if click:
        h["Click"] = click
    req = urllib.request.Request(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode(), headers=h)
    urllib.request.urlopen(req, timeout=20).read()

def norm_key(j):
    return j["company"].lower() + "|" + re.sub(r"[^a-z0-9]+", " ", j["title"].lower()).strip()

def main():
    first_run = not SEEN_FILE.exists()
    seen = json.loads(SEEN_FILE.read_text()) if not first_run else {"ids": [], "titles": []}
    seen_ids, seen_titles = set(seen["ids"]), set(seen["titles"])
    # companies seen before; a newly added company is saved quietly on its first run
    known_companies = set(seen.get("companies") or {t.split("|")[0] for t in seen_titles})
    quiet, fetched_companies = [], set()
    status, new = {}, []
    for name, fn in SOURCES.items():
        try:
            jobs = fn()
            fetched_companies |= {j["company"].lower() for j in jobs}
            matches = [j for j in jobs if title_matches(j["title"]) and location_ok(j["location"])]
            status[name] = {"ok": True, "fetched": len(jobs), "matching": len(matches)}
            for j in matches:
                if j["id"] in seen_ids or norm_key(j) in seen_titles:
                    continue
                c = j["company"].lower()
                is_newly_added = c in DIRECT_COMPANIES and c not in known_companies
                (quiet if is_newly_added else new).append(j)
                seen_ids.add(j["id"]); seen_titles.add(norm_key(j))
        except Exception as e:  # one broken site never stops the others
            status[name] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        print(name, status[name])

    if quiet:
        print(f"Quietly saved {len(quiet)} postings from newly added companies")
    if first_run:
        notify("Job alerts are on", f"Now watching {len(new) + len(quiet)} matching postings. "
               "You'll get a ping for each new one from here on.", tags="white_check_mark")
    else:
        for j in new[:25]:
            notify(f"{j['company']}: {j['title']}"[:200], f"{j['location']}\n{j['url']}", click=j["url"] or None)
        if len(new) > 25:
            notify("More new postings", f"{len(new) - 25} more matches this round; check the career sites.")

    if not DRY_RUN:
        companies = known_companies | fetched_companies
        SEEN_FILE.write_text(json.dumps({"ids": sorted(seen_ids), "titles": sorted(seen_titles),
                                         "companies": sorted(companies)}, indent=0))
        STATUS_FILE.write_text(json.dumps({"checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                           "new_this_run": len(new), "sources": status}, indent=2))
    else:
        print(f"\n{len(new)} matches (dry run):")
        for j in new:
            print(f"- {j['company']}: {j['title']} | {j['location'][:60]} | {j['url']}")

if __name__ == "__main__":
    main()
