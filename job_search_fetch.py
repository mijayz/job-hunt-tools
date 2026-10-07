#!/usr/bin/env python3
"""Weekly job-lead fetcher. Stdlib only.

Usage:  python3 job_search_fetch.py [--known known.json] [--only Medpace,IQVIA] [--out leads.json]

known.json = list of {"url","company","role"} (or Notion names "Application URL","Company","Role") for EVERY row in Notion "Job leads", any Status.
Output: leads.json (survivors of location + title filters, minus already-known) and a per-source log on stderr.
Hard filters applied here: location (London / UK-remote), title (Senior/Specialist/etc.). Salary, sponsorship wording and
experience bucket still need the advert text, so the survivors are what you read.
"""
import argparse, json, re, subprocess, sys, html, urllib.parse
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0"
TITLE_BAD = re.compile(r"\b(senior|sr\.?|specialist|principal|lead|head|director|manager|consultant|vp|vice president|intern(ship)?|III|IV|V|3)\b", re.I)
SCOPE_BAD = re.compile(r"nurse|physician|psychiatrist|pharmacist|surgeon|gastroenterolog|hepatolog|oncologist|dermatolog|hematolog|haematolog|pulmonolog|medical monitor|\bchef\b|housekeep|\bsecurity\b|engineer|developer|architect|programmer|devops|\bfinance\b|payroll|\bsales\b|business development|account (executive|manager)|marketing|recruiter|talent acquisition|recruitment (consultant|partner|advisor|business partner)|translator|\brater\b|professor|lecturer|chaplain|technician|electrician|career event|talent community|test req|do not apply|post-?doc|doctoral|fellowship|\b(?!english)[a-z]+[- ]speak(ing|er)\b|\(phd\)|\bexperienced\b|data scientist|statistician|biostatistician|freelance", re.I)
KEEP_HINT = re.compile(r"research|clinical|trial|study|coordinator|officer|assistant|analyst|project|programme|data|writer|editor|safety|pharmacovigilance|regulatory|medical|feasib|associate|administrator", re.I)   # used for broad university feeds only
TITLE_OK_PHRASE = re.compile(r"\b(associate|assistant)\b[^/,(]*\bmanager\b", re.I)   # "Associate ... Manager" allowed
LOC_OK = re.compile(r"london|hammersmith|twickenham|greater london|uk[\s\-–—]*remote|remote[\s,\-–—]*(uk|united kingdom|gb)|united kingdom[\s\-–—]*remote|virtual united kingdom|home[- ]?based.*uk|^united kingdom$|^uk$|^gb$|, gb( remote)?$", re.I)

def sh(args, post=None, timeout=60, headers=()):
    c = ["curl", "-s", "-m", str(timeout), "-L", "-A", UA, "-w", "\n%{http_code}"]
    for h in headers: c += ["-H", h]
    if post is not None: c += ["-X", "POST", "-H", "content-type: application/json", "-d", post]
    out = subprocess.run(c + [args], capture_output=True, text=True).stdout
    body, _, code = out.rpartition("\n")
    return code, body

def jget(url, post=None, headers=()):
    code, body = sh(url, post, headers=headers)
    if code != "200": raise RuntimeError(f"HTTP {code}")
    return json.loads(body)

def title_ok(t):
    t2 = TITLE_OK_PHRASE.sub("", t)
    return not TITLE_BAD.search(t2) and not SCOPE_BAD.search(t)

REMOTE_TITLE = re.compile(r"^(?=.*\b(uk|united kingdom)\b)(?=.*\b(remote|home[- ]based)\b)", re.I)
def loc_ok(l, title=""):
    parts = [p.strip() for p in re.split(r";|\|", l or "")]
    if any(LOC_OK.search(p) for p in parts) or LOC_OK.search(l or ""): return True
    return bool(re.search(r"united kingdom|england|\buk\b", l or "", re.I) and REMOTE_TITLE.search(title or ""))

KEEP_PARAMS = {"jk", "vjk", "p_recruitment_id", "gh_jid", "jobid", "job_id", "id", "reqid", "requisitionid"}   # params that ARE the job identity
def strip(u):
    """Drop tracking params/fragments but keep params that identify the job (Indeed jk, KCL p_recruitment_id, ...)."""
    u = (u or "").strip().split("#")[0]
    m = re.search(r"medpace[^/]*/(?:[^?]*/)?jobs/(\d+)", u)          # careers./talent./international-medpace.icims -> one canonical form
    if m: return f"https://talent.medpace.com/jobs/{m.group(1)}"
    if "?" not in u: return u.rstrip("/")
    base, q = u.split("?", 1)
    keep = [(k, v) for k, v in urllib.parse.parse_qsl(q, keep_blank_values=True) if k.lower() in KEEP_PARAMS]
    return base.rstrip("/") + ("?" + urllib.parse.urlencode(sorted(keep)) if keep else "")
def norm(s): return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
COMPANY_ALIASES = {   # any of these tokens anywhere in the company name -> canonical key
 "thermo": ("ppd", "thermo", "evidera"), "inizio": ("inizio", "nucleus", "ashfield", "apothecom", "evoke"),
 "ergomed": ("primevigilance", "ergomed"), "phastar": ("phastar", "vivos"), "kcl": ("king s college", "kcl"),
 "novotech": ("novotech",), "iqvia": ("iqvia",), "labcorp": ("labcorp",), "medpace": ("medpace",), "icon": ("icon clinical", "icon plc", "icon"),
}
STOP = {"the", "ltd", "limited", "plc", "llp", "uk", "inc", "group"}
def ckey(c):
    n = norm(c)
    for k, toks in COMPANY_ALIASES.items():
        if any(re.search(rf"\b{t}\b", n) for t in toks): return k
    words = [w for w in n.split() if w not in STOP]
    return words[0] if words else n

# ---------- fetchers: each returns list of dicts {company,title,location,url,posted} ----------
def workday(company, host, tenant, site, search="United Kingdom"):
    out, off, total = [], 0, None
    while True:
        d = jget(f"https://{host}/wday/cxs/{tenant}/{site}/jobs", json.dumps({"appliedFacets": {}, "limit": 20, "offset": off, "searchText": search}))
        if total is None: total = d.get("total", 0)          # total is only reliable on the first page
        ps = d.get("jobPostings", [])
        for p in ps:
            out.append(dict(company=company, title=p["title"], location=p.get("locationsText", ""), posted=p.get("postedOn"),
                            url=f"https://{host}/en-US/{site}{p['externalPath']}", _detail=f"https://{host}/wday/cxs/{tenant}/{site}{p['externalPath']}"))
        off += 20
        if not ps or off >= total: break
    for j in out:   # expand "N Locations"
        if re.match(r"\d+ Locations", j["location"]) and title_ok(j["title"]):
            try:
                i = jget(j["_detail"])["jobPostingInfo"]
                j["location"] = "; ".join([i.get("location", "")] + i.get("additionalLocations", []))
            except Exception: pass
    return out

def oracle(company, host, site):
    d = jget(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList&finder=findReqs;siteNumber={site},limit=200")
    return [dict(company=company, title=r["Title"], location=r.get("PrimaryLocation", ""), posted=r.get("PostedDate"),
                 url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{r['Id']}") for r in d["items"][0]["requisitionList"]]

def smartrecruiters(company, slug):
    d = jget(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&country=gb")
    out = []
    for p in d["content"]:
        l = p["location"]; loc = f"{l.get('city','')}, {l.get('country','')}" + (" remote" if l.get("remote") else "")
        out.append(dict(company=company, title=p["name"], location=loc.replace(", gb", ", GB"), posted=p.get("releasedDate"), url=f"https://jobs.smartrecruiters.com/{slug}/{p['id']}"))
    return out

def greenhouse(company, board):
    d = jget(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=false")
    return [dict(company=company, title=j["title"].strip(), location=j["location"]["name"], posted=j.get("updated_at"), url=j["absolute_url"]) for j in d["jobs"]]

def ashby(company, slug):
    d = jget(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    return [dict(company=company, title=j["title"].strip(), location=(j.get("location") or "") + ("; UK remote" if j.get("isRemote") and "United Kingdom" in str(j.get("location")) else ""), posted=j.get("publishedAt"), url=j["jobUrl"]) for j in d["jobs"]]

def lever(company, slug):
    d = jget(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    return [dict(company=company, title=j["text"], location=j["categories"].get("location") or "", posted=j.get("createdAt"), url=j["hostedUrl"]) for j in d]

def jibe(company, base, location="London"):
    out, page = [], 1
    while True:
        d = jget(f"{base}/api/jobs?location={urllib.parse.quote(location)}&limit=100&page={page}")
        js = d.get("jobs", [])
        for j in js:
            x = j.get("data", j)
            u = x.get("canonical_url") or x.get("apply_url") or ""
            out.append(dict(company=company, title=x.get("title", ""), location=x.get("full_location") or x.get("location_name") or "", posted=x.get("posted_date"), url=strip(u) if "medpace" in u else u))
        if not js or page * 100 >= d.get("totalCount", 0): break
        page += 1
    return out

def icims(company, host, pages=6):
    """Server-rendered iCIMS search. Locations are not in the list view, so location is left blank and flagged for manual check."""
    out, seen = [], set()
    for pr in range(pages):
        code, b = sh(f"https://{host}/jobs/search?ss=1&in_iframe=1&pr={pr}")
        if code != "200":
            if pr == 0: raise RuntimeError(f"HTTP {code}")
            break
        found = re.findall(r'href="([^"]*?/jobs/(\d+)/([^"/]+)/job[^"]*)"', b)
        new = [(h, i, s) for h, i, s in found if i not in seen]
        if not new: break
        for h, i, s in new:
            seen.add(i)
            out.append(dict(company=company, title=urllib.parse.unquote(s).replace("-", " ").strip().title(), location="?iCIMS-check", url=f"https://{host}/jobs/{i}/{s}/job"))
    return out

def icon_html(company="ICON", pages=40):
    """careers.iconplc.com: Jibe-style HTML, 12 jobs/page, no JSON API. Country is encoded in the URL slug (-in-uk-<site>-jid-N)."""
    out, seen = [], set()
    for p in range(1, pages + 1):
        code, b = sh(f"https://careers.iconplc.com/jobs?page={p}")
        if code != "200":
            if p == 1: raise RuntimeError(f"HTTP {code}")
            break
        js = [j for j in re.findall(r'href="(/job/[^"]+)"', b) if j not in seen]
        if not js: break
        for j in js:
            seen.add(j)
            if re.search(r"-in-(uk|regional-great-britain|united-kingdom)", j):
                slug = re.sub(r"^/job/|-jid-\d+$", "", j)
                title = re.split(r"-in-(?:uk|regional|united)", slug)[0].replace("-", " ").title()
                site = re.search(r"-in-(?:uk|regional-great-britain-northern-ireland|united-kingdom)-?(.*)$", slug)
                out.append(dict(company=company, title=title, location=("UK " + (site.group(1).replace("-", " ").title() if site and site.group(1) else "")).strip(), url="https://careers.iconplc.com" + j))
    return out

def tfs_rss(company="TFS Trial Form Support Limited"):
    code, b = sh("https://careers.tfscro.com/jobs.rss")
    if code != "200": raise RuntimeError(f"HTTP {code}")
    out = []
    for it in re.findall(r"<item>(.*?)</item>", b, re.S):
        t = re.search(r"<title>(.*?)</title>", it, re.S); l = re.search(r"<link>(.*?)</link>", it, re.S)
        locs = ["%s, %s" % (ci.strip(), co.strip()) for ci, co in re.findall(r"<tt:city>(.*?)</tt:city>\s*<tt:country>(.*?)</tt:country>", it, re.S)]
        remote = re.search(r"<remoteStatus>(fully|hybrid)</remoteStatus>", it)
        loc = "; ".join(locs) + ("; UK remote" if remote and remote.group(1) == "fully" and any("United Kingdom" in x for x in locs) else "")
        out.append(dict(company=company, title=html.unescape(t.group(1).strip()) if t else "", location=loc, url=(l.group(1).strip() if l else "")))
    return out

def cogora_rss(company="Cogora"):
    code, b = sh("https://www.cogora.com/careers/feed/")
    if code != "200": raise RuntimeError(f"HTTP {code}")
    return [dict(company=company, title=html.unescape(t), location="London?", url=l) for t, l in re.findall(r"<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>", b, re.S)]

def kcl():
    where = '[{"field":"sys.versionStatus","equalTo":"published"},{"field":"sys.contentTypeId","equalTo":"jobVacancy"}]'   # widened: all types/faculties
    d = jget("https://www.kcl.ac.uk/api/delivery/projects/website/entries/search?linkDepth=1&pageIndex=0&pageSize=200&where=" + urllib.parse.quote(where),
             headers=("accept: application/json", "accesstoken: JjV9NgvYm8BQgTNx2AtThsRBeK5qZxArDnRc2SKrzYWzvsS6"))
    return [dict(company="King's College London", title=e.get("entryTitle", ""), location="London", posted=e.get("recruitExternalOpenDate"), url=e.get("applyURL", ""), salary=e.get("gradeAndSalaryText")) for e in d["items"]]

def jobs_ac_uk(keywords=("clinical research", "research assistant", "clinical trial", "research coordinator", "pharmacovigilance", "project officer", "programme coordinator", "trial coordinator", "study coordinator", "research administrator", "data coordinator")):
    out = {}
    for k in keywords:
        code, b = sh(f"https://www.jobs.ac.uk/search/?keywords={urllib.parse.quote_plus(k)}&location=London&sortOrder=1&pageSize=50")
        if code != "200": raise RuntimeError(f"HTTP {code} on keyword '{k}'")
        for blk in re.findall(r'<div class="j-search-result__text">(.*?)<div class="j-search-result__date-logos">', b, re.S):
            u = re.search(r'href="(/job/[^"]+)"', blk); t = re.search(r'href="[^"]+">(.*?)</a>', blk, re.S)
            emp = re.search(r'__employer">\s*<b>(.*?)</b>', blk, re.S); loc = re.search(r"Location:\s*(.*?)</div>", blk, re.S); sal = re.search(r"Salary: </strong>\s*(.*?)\s*</div>", blk, re.S)
            if u:
                out["https://www.jobs.ac.uk" + u.group(1)] = dict(company=html.unescape((emp.group(1) if emp else "").strip()), title=html.unescape(re.sub(r"<[^>]+>", "", t.group(1))).strip(),
                    location=" ".join((loc.group(1) if loc else "").split()), salary=" ".join(re.sub(r"<[^>]+>", "", sal.group(1)).split()) if sal else "", url="https://www.jobs.ac.uk" + u.group(1))
    return list(out.values())

SOURCES = {
 "LabCorp":      lambda: workday("LabCorp Early Development Laboratories Limited", "labcorp.wd1.myworkdayjobs.com", "labcorp", "External"),
 "ThermoFisher": lambda: workday("PPD Global Ltd / Evidera (Thermo Fisher)", "thermofisher.wd5.myworkdayjobs.com", "thermofisher", "ThermoFisherCareers"),
 "WorldWide":    lambda: workday("WorldWide Clinical Trials", "worldwide.wd1.myworkdayjobs.com", "worldwide", "External"),
 "IQVIA":        lambda: workday("IQVIA Limited", "iqvia.wd1.myworkdayjobs.com", "iqvia", "IQVIA", "London") + workday("IQVIA Limited", "iqvia.wd1.myworkdayjobs.com", "iqvia", "IQVIA", "Remote UK"),
 "Fortrea":      lambda: workday("Fortrea Development Limited", "fortrea.wd1.myworkdayjobs.com", "fortrea", "Fortrea", "United Kingdom"),
 "Veristat":     lambda: workday("Veristat International Limited", "veristat.wd503.myworkdayjobs.com", "veristat", "Veristatcareers", "United Kingdom"),
 "Cytel":        lambda: oracle("Cytel", "iblyjb.fa.ocs.oraclecloud.com", "cytel"),
 "Novotech":     lambda: oracle("Novotech Clinical Research UK Ltd", "fa-euzi-saasfaprod1.fa.ocs.oraclecloud.com", "CX_1"),
 "Ergomed":      lambda: smartrecruiters("PrimeVigilance Ltd", "Ergomed"),
 "GenomicsEng":  lambda: smartrecruiters("Genomics England Limited", "GenomicsEngland"),
 "Lindus":       lambda: ashby("Lindus Health Limited", "lindus"),
 "Avalere":      lambda: lever("Avalere Health Global Limited", "avalerehealth"),
 "Inizio":       lambda: sum([greenhouse(c, b) for c, b in [("Inizio (corporate)", "inizio"), ("Nucleus Global (Inizio)", "nucleus"), ("Ashfield MedComms (Inizio)", "ashfieldmedcomms"),
                                                              ("ApotheCom (Inizio)", "apothecom"), ("Inizio Engage / xd", "xdinizioengage")]], []),
 "Medpace":      lambda: jibe("Medpace UK Limited", "https://talent.medpace.com"),
 "Emmes":        lambda: jibe("Emmes Biopharma UK Ltd", "https://careers.emmes.com"),
 "ICON":         lambda: icon_html(),
 "Phastar":      lambda: icims("Vivos Technology Limited (PHASTAR)", "careers-phastar.icims.com"),
 "Lumanity":     lambda: icims("Lumanity Limited", "emeacareers-lumanity.icims.com"),
 "TFS":          lambda: tfs_rss(),
 "Cogora":       lambda: cogora_rss(),
 "KCL":          lambda: kcl(),
 "jobs.ac.uk":   lambda: jobs_ac_uk(),
}

BROAD = {"KCL", "jobs.ac.uk"}   # feeds that cover the whole university; also need a relevance hint in the title

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--known"); ap.add_argument("--only"); ap.add_argument("--out", default="leads.json")
    a = ap.parse_args()
    known = json.load(open(a.known)) if a.known else []
    g = lambda k, *names: next((k[n] for n in names if k.get(n)), "")
    kurl = {strip(g(k, "url", "Application URL")) for k in known if g(k, "url", "Application URL")}
    kcr = {(ckey(g(k, "company", "Company")), norm(g(k, "role", "Role"))) for k in known}
    print(f"known: {len(known)} rows, {len(kurl)} urls", file=sys.stderr)
    names = a.only.split(",") if a.only else list(SOURCES)
    def run(n):
        try: return n, SOURCES[n](), None
        except Exception as e: return n, [], str(e)[:120]
    leads, log, seen = [], [], set()
    with ThreadPoolExecutor(6) as ex:
        for n, jobs, err in ex.map(run, names):
            if err: log.append((n, "FAILED", err)); continue
            c = dict(loc=0, title=0, known=0, kept=0)
            for j in jobs:
                if not (loc_ok(j["location"], j["title"]) or j["location"].startswith("?")): c["loc"] += 1; continue
                if not title_ok(j["title"]) or (n in BROAD and not KEEP_HINT.search(j["title"])): c["title"] += 1; continue
                if strip(j["url"]) in kurl or (ckey(j["company"]), norm(j["title"])) in kcr: c["known"] += 1; continue
                if strip(j["url"]) in seen: continue
                seen.add(strip(j["url"]))
                c["kept"] += 1; j["source"] = n; j.pop("_detail", None); leads.append(j)
            log.append((n, f"{len(jobs)} found", f"filtered: {c['loc']} location, {c['title']} title, {c['known']} already logged; kept {c['kept']}"))
    json.dump(leads, open(a.out, "w"), indent=1)
    for n, a1, b1 in log: print(f"{n:14} {a1:14} {b1}", file=sys.stderr)
    for j in leads:
        try: print(f"{j['source']:12} | {j['title'][:70]:70} | {j['location'][:50]:50} | {j['url']}")
        except BrokenPipeError: break

if __name__ == "__main__": main()
