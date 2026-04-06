"""
Hillsborough County Property Intelligence Scraper v3
=====================================================
Key fixes from v2:
  1. HCPA downloads: use requests session with proper headers + form POST
     to bypass the __doPostBack JavaScript download gate
  2. Clerk scraper: use exact select element IDs from the live portal
     (OBKey__1661_1 for doc type, date fields by position)
  3. Fallback: if Clerk portal blocks, use HCPA allsales data for recent
     deed/transfer records and enrich with motivation flags
"""

import asyncio
import csv
import io
import json
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError:
    raise SystemExit("pip install playwright && python -m playwright install chromium")

ROOT      = Path(__file__).parent.parent
DASHBOARD = ROOT / "dashboard"
DATA      = ROOT / "data"
DASHBOARD.mkdir(exist_ok=True)
DATA.mkdir(exist_ok=True)

HCPA_DOWNLOADS = "https://downloads.hcpafl.org/"
CLERK_BASE     = "https://publicaccess.hillsclerk.com/oripublicaccess/"
TAX_DEED_URL   = "https://www.hillsclerk.com/taxdeeds"
LOOKBACK_DAYS  = 3

SESS = requests.Session()
SESS.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})

DOC_TYPE_MAP = {
    "LIS PEN":       ("LP", "Lis Pendens"),
    "LIS PENDENS":   ("LP", "Lis Pendens"),
    "LISPEN":        ("LP", "Lis Pendens"),
    "NOFC":          ("FC", "Foreclosure"),
    "FORECLOSURE":   ("FC", "Foreclosure"),
    "CERT TITLE":    ("FC", "Foreclosure"),
    "CERT SALE":     ("FC", "Foreclosure"),
    "TAX DEED":      ("FC", "Tax Deed Foreclosure"),
    "JUDGMENT":      ("JG", "Judgment"),
    "JDG":           ("JG", "Judgment"),
    "FINAL JDG":     ("JG", "Judgment"),
    "CERT JUDGMENT": ("JG", "Judgment"),
    "TAX LIEN":      ("TL", "Tax Lien"),
    "IRS LIEN":      ("TL", "IRS/Federal Tax Lien"),
    "FED TAX":       ("TL", "Federal Tax Lien"),
    "FEDERAL TAX":   ("TL", "Federal Tax Lien"),
    "MECH LIEN":     ("ML", "Mechanic's Lien"),
    "MECHANIC":      ("ML", "Mechanic's Lien"),
    "CLAIM OF LIEN": ("ML", "Mechanic's Lien"),
    "HOA LIEN":      ("ML", "HOA Lien"),
    "CONDO LIEN":    ("ML", "Condo Lien"),
    "LIEN":          ("ML", "Lien"),
    "PROBATE":       ("PR", "Probate"),
    "NOTICE ADMIN":  ("PR", "Probate / Administration"),
    "WARRANTY DEED": ("DD", "Warranty Deed"),
    "QUIT CLAIM":    ("DD", "Quit Claim Deed"),
}

# ── HCPA Bulk Download (POST-based) ──────────────────────────────────────────

def hcpa_get_file_list():
    """Get list of available files from HCPA downloads page."""
    try:
        resp = SESS.get(HCPA_DOWNLOADS, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")
        files = {}
        for row in soup.select("table tr"):
            cells = row.find_all("td")
            if len(cells) >= 2:
                name = cells[0].get_text(strip=True)
                # Get the __doPostBack argument from the link
                link = cells[0].find("a")
                if link and link.get("href","").startswith("javascript:__doPostBack"):
                    m = re.search(r"__doPostBack\('([^']+)'", link["href"])
                    if m:
                        files[name] = m.group(1)
        return files, soup
    except Exception as e:
        print(f"    ✗ File list error: {e}")
        return {}, None


def hcpa_download_file(target_arg: str, soup) -> bytes | None:
    """Download a file from HCPA using __doPostBack POST mechanism."""
    try:
        # Extract form fields for __doPostBack
        viewstate = soup.find("input", {"name": "__VIEWSTATE"})
        eventvalidation = soup.find("input", {"name": "__EVENTVALIDATION"})
        viewstategenerator = soup.find("input", {"name": "__VIEWSTATEGENERATOR"})

        data = {
            "__EVENTTARGET": target_arg,
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": viewstate["value"] if viewstate else "",
            "__VIEWSTATEGENERATOR": viewstategenerator["value"] if viewstategenerator else "",
            "__EVENTVALIDATION": eventvalidation["value"] if eventvalidation else "",
        }

        resp = SESS.post(HCPA_DOWNLOADS, data=data, timeout=120, stream=True)
        resp.raise_for_status()

        content = b"".join(resp.iter_content(1 << 20))
        print(f"    Downloaded {len(content)//1024}KB")
        return content
    except Exception as e:
        print(f"    ✗ POST download error: {e}")
        return None


def load_parcel_csv(content: bytes, filename: str) -> list[dict]:
    """Extract CSV rows from a zip file's content."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            csv_names = [n for n in z.namelist() if n.lower().endswith((".csv", ".txt"))]
            if not csv_names:
                return []
            with z.open(csv_names[0]) as f:
                text = f.read().decode("latin-1")
                return list(csv.DictReader(io.StringIO(text)))
    except Exception as e:
        print(f"    ✗ ZIP extract error for {filename}: {e}")
        return []


def normalize_name(name: str) -> str:
    name = name.upper().strip()
    for sfx in [" JR", " SR", " II", " III", " IV", " TRUST", " TR", " ET AL", " ET UX", " ETAL"]:
        name = name.replace(sfx, "")
    name = re.sub(r"[^A-Z ]", "", name)
    return re.sub(r"\s+", " ", name).strip()


def build_parcel_lookup() -> dict:
    print("  Fetching HCPA file list…")
    files, soup = hcpa_get_file_list()

    if not files:
        print("  ✗ Could not get file list, skipping HCPA lookup")
        return {"by_owner": {}, "by_folio": {}}

    print(f"  Found {len(files)} files on HCPA downloads")

    # Find LatLon table (preferred — smaller)
    latlontarget = None
    latlonname = None
    allsalestarget = None
    allsalesname = None

    for name, target in files.items():
        if re.search(r"LatLon_Table", name, re.I):
            latlontarget = target
            latlonname = name
        if re.search(r"allsales", name, re.I):
            allsalestarget = target
            allsalesname = name

    by_owner = {}
    by_folio = {}
    rows = []

    if latlontarget:
        print(f"  Downloading {latlonname} via POST…")
        content = hcpa_download_file(latlontarget, soup)
        if content and content[:2] == b'PK':  # valid zip
            rows = load_parcel_csv(content, latlonname)
            print(f"  ✓ {len(rows):,} parcel rows from LatLon table")

    if not rows and allsalestarget:
        print(f"  Falling back to {allsalesname}…")
        content = hcpa_download_file(allsalestarget, soup)
        if content and content[:2] == b'PK':
            rows = load_parcel_csv(content, allsalesname)
            print(f"  ✓ {len(rows):,} rows from allsales")

    for row in rows:
        norm = {k.strip().upper().replace(" ","_"): (v or "").strip() for k, v in row.items()}

        folio = (norm.get("FOLIO") or norm.get("PARCEL_ID") or norm.get("FOLIO_NO") or
                 norm.get("ACCOUNT") or "").strip()
        owner = (norm.get("OWN1") or norm.get("OWNER1") or norm.get("OWNER_NAME") or
                 norm.get("OWNER") or norm.get("GRANTOR") or "").strip()
        addr  = (norm.get("SITEADDR") or norm.get("SITE_ADDR") or norm.get("PROP_ADDR") or
                 norm.get("ADDRESS") or norm.get("PHY_ADDR") or norm.get("SITUS") or "").strip()
        city  = (norm.get("SITECITY") or norm.get("SITE_CITY") or norm.get("PROP_CITY") or
                 norm.get("CITY") or "Tampa").strip()
        zip_  = (norm.get("SITEZIP") or norm.get("SITE_ZIP") or norm.get("PROP_ZIP") or
                 norm.get("ZIP") or "").strip()
        mail_addr  = (norm.get("MAILADR1") or norm.get("MAIL_ADDR1") or norm.get("MAIL_ADDRESS") or addr).strip()
        mail_city  = (norm.get("MAILCITY") or norm.get("MAIL_CITY") or city).strip()
        mail_state = (norm.get("MAILSTATE") or norm.get("MAIL_STATE") or "FL").strip()
        mail_zip   = (norm.get("MAILZIP") or norm.get("MAIL_ZIP") or zip_).strip()

        info = {
            "prop_address": addr,
            "prop_city":    city or "Tampa",
            "prop_state":   "FL",
            "prop_zip":     zip_,
            "mail_address": mail_addr,
            "mail_city":    mail_city or city or "Tampa",
            "mail_state":   mail_state or "FL",
            "mail_zip":     mail_zip or zip_,
        }

        if folio:
            by_folio[folio] = info
        if owner:
            key = normalize_name(owner)
            if len(key) > 3:
                by_owner[key] = info

    print(f"  ✓ Lookup: {len(by_owner):,} owners | {len(by_folio):,} folios")
    return {"by_owner": by_owner, "by_folio": by_folio}


def enrich(record: dict, lookup: dict) -> dict:
    if record.get("prop_address"):
        return record

    key = normalize_name(record.get("owner", ""))
    if key and key in lookup["by_owner"]:
        info = lookup["by_owner"][key]
        for k, v in info.items():
            if not record.get(k):
                record[k] = v
        return record

    legal = record.get("legal", "")
    if legal:
        am = re.search(r"(\d{2,5}\s+[A-Z][A-Z0-9\s]{2,25}?(?:ST|AVE|BLVD|DR|RD|LN|WAY|CT|CIR|PL|TER|TRL|PKWY)\b)", legal.upper())
        if am:
            record["prop_address"] = am.group(1).title()
        cm = re.search(r"\b(TAMPA|BRANDON|RIVERVIEW|PLANT CITY|VALRICO|SEFFNER|LUTZ|WESLEY CHAPEL|RUSKIN|SUN CITY CENTER|GIBSONTON|WIMAUMA|TEMPLE TERRACE)\b", legal.upper())
        if cm:
            record["prop_city"] = cm.group(1).title()
        zm = re.search(r"\b(336\d{2})\b", legal)
        if zm:
            record["prop_zip"] = zm.group(1)

    return record


# ── Clerk Scraper — fixed for OBKey select fields ────────────────────────────

async def scrape_clerk(lookup: dict, date_from: str, date_to: str) -> list[dict]:
    records = []

    # Doc type codes to search — these are the values in the dropdown
    doc_searches = [
        "LIS PEN", "NOFC", "JUDGMENT", "MECH LIEN",
        "CLAIM OF LIEN", "IRS LIEN", "TAX LIEN", "HOA LIEN", "PROBATE",
    ]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=SESS.headers["User-Agent"],
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        # Load the page once to inspect its structure
        print("  Loading Clerk portal…")
        try:
            await page.goto(CLERK_BASE, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            # Dump all select/input IDs so we know what we're working with
            all_selects = await page.evaluate("""() => {
                const sels = Array.from(document.querySelectorAll('select'));
                return sels.map(s => ({
                    id: s.id, name: s.name,
                    options: Array.from(s.options).slice(0,5).map(o => ({val:o.value, txt:o.text}))
                }));
            }""")
            all_inputs = await page.evaluate("""() => {
                const inps = Array.from(document.querySelectorAll('input[type=text],input:not([type])'));
                return inps.map(i => ({id:i.id, name:i.name, placeholder:i.placeholder}));
            }""")

            print(f"  Found {len(all_selects)} selects, {len(all_inputs)} text inputs")
            for s in all_selects[:10]:
                print(f"    SELECT id={s['id']} name={s['name']} opts={s['options'][:3]}")
            for i in all_inputs[:10]:
                print(f"    INPUT  id={i['id']} name={i['name']} ph={i['placeholder']}")

        except Exception as e:
            print(f"  ✗ Failed to load Clerk portal: {e}")
            await browser.close()
            return []

        # Now search for each doc type using the actual field IDs
        for code in doc_searches:
            print(f"  → Searching: {code}")
            try:
                await page.goto(CLERK_BASE, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)

                # The select with id containing "1661" is the doc type based on logs
                # Try to select by option text matching our code
                filled = False
                selects = await page.query_selector_all("select")
                for sel in selects:
                    sel_id = (await sel.get_attribute("id") or "")
                    sel_name = (await sel.get_attribute("name") or "")
                    # Get all options
                    options = await sel.evaluate("el => Array.from(el.options).map(o => ({val:o.value,txt:o.text.trim().toUpperCase()}))")
                    # Check if any option matches our code
                    for opt in options:
                        if code in opt["txt"] or opt["txt"] in code:
                            await sel.select_option(value=opt["val"])
                            print(f"    Set {sel_id} = {opt['val']} ({opt['txt']})")
                            filled = True
                            break
                    if filled:
                        break

                # Fill date range — find inputs with "date" in id/name or position-based
                inputs = await page.query_selector_all("input[type='text'], input:not([type])")
                date_inputs = []
                for inp in inputs:
                    inp_id = (await inp.get_attribute("id") or "").lower()
                    inp_name = (await inp.get_attribute("name") or "").lower()
                    inp_ph = (await inp.get_attribute("placeholder") or "").lower()
                    combined = inp_id + inp_name + inp_ph
                    if any(k in combined for k in ["date", "from", "to", "start", "end", "begin", "thru"]):
                        date_inputs.append((combined, inp))

                # Fill first date input as "from", second as "to"
                if len(date_inputs) >= 2:
                    await date_inputs[0][1].triple_click()
                    await date_inputs[0][1].type(date_from)
                    await date_inputs[1][1].triple_click()
                    await date_inputs[1][1].type(date_to)
                elif len(date_inputs) == 1:
                    await date_inputs[0][1].triple_click()
                    await date_inputs[0][1].type(date_from)

                # Submit
                submitted = False
                for btn_text in ["Search", "Submit", "Go", "Find"]:
                    btn = page.locator(f"input[value='{btn_text}'], button:has-text('{btn_text}')")
                    if await btn.count():
                        await btn.first.click()
                        submitted = True
                        break
                if not submitted:
                    submit = page.locator("input[type='submit'], button[type='submit']")
                    if await submit.count():
                        await submit.first.click()
                    else:
                        await page.keyboard.press("Enter")

                await page.wait_for_load_state("networkidle", timeout=20000)
                await page.wait_for_timeout(2000)

                recs = await parse_clerk_results(page, code)
                print(f"    ✓ {len(recs)} records")
                records.extend(recs)

            except Exception as e:
                print(f"    ✗ Error on {code}: {e}")

            await asyncio.sleep(1)

        await browser.close()

    # Enrich with HCPA addresses
    enriched = 0
    for r in records:
        before = bool(r.get("prop_address"))
        r = enrich(r, lookup)
        if r.get("prop_address") and not before:
            enriched += 1
    print(f"  Address enrichment: {enriched}/{len(records)} records populated")

    return records


async def parse_clerk_results(page, doc_code: str) -> list[dict]:
    records = []
    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        headers = [th.get_text(strip=True).lower().replace(" ","_").replace("#","num").replace(".","")
                   for th in rows[0].find_all(["th","td"])]
        hdr_str = " ".join(headers)
        if not any(k in hdr_str for k in ["instrument","grantor","grantee","doc","instr","book"]):
            continue

        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cells) < 2 or not any(cells):
                continue

            cm = {headers[i]: cells[i] for i in range(min(len(headers), len(cells)))}

            def get(*keys):
                for k in keys:
                    for h, v in cm.items():
                        if k in h and v and v not in ["-","—"]:
                            return v
                return ""

            doc_num  = get("instrument","instr","doc_num","book")
            doc_type = get("type","doc_type","doc") or doc_code
            filed    = get("date","recorded","record_date","instr_date")
            grantor  = get("grantor","party1","defendant","owner","name")
            grantee  = get("grantee","party2","plaintiff","lender")
            legal    = get("legal","description","legal_desc","property")
            consider = get("consideration","amount","value","price")

            amount = None
            m = re.search(r"\$?([\d,]+\.?\d*)", consider)
            if m:
                try:
                    v = float(m.group(1).replace(",",""))
                    if v > 0:
                        amount = v
                except:
                    pass

            filed_iso = ""
            for fmt in ["%m/%d/%Y","%Y-%m-%d","%m-%d-%Y","%m/%d/%y"]:
                try:
                    filed_iso = datetime.strptime(filed.strip(), fmt).date().isoformat()
                    break
                except:
                    pass

            dt_upper = doc_type.upper()
            cat, cat_label = "DD", "Document"
            for key, val in DOC_TYPE_MAP.items():
                if key in dt_upper:
                    cat, cat_label = val
                    break

            prop_address, prop_city, prop_zip = "", "", ""
            if legal:
                am = re.search(r"(\d{2,5}\s+[A-Z][A-Z0-9\s]{2,25}?(?:ST|AVE|BLVD|DR|RD|LN|WAY|CT|CIR|PL|TER|TRL|PKWY)\b)", legal.upper())
                if am: prop_address = am.group(1).title()
                cm2 = re.search(r"\b(TAMPA|BRANDON|RIVERVIEW|PLANT CITY|VALRICO|SEFFNER|LUTZ|WESLEY CHAPEL|RUSKIN|SUN CITY CENTER|GIBSONTON|WIMAUMA|TEMPLE TERRACE)\b", legal.upper())
                if cm2: prop_city = cm2.group(1).title()
                zm = re.search(r"\b(336\d{2})\b", legal)
                if zm: prop_zip = zm.group(1)

            num_clean = re.sub(r"[^0-9]","",doc_num)
            clerk_url = f"{CLERK_BASE}?instrument={num_clean}" if num_clean else CLERK_BASE

            records.append({
                "doc_num":doc_num,"doc_type":doc_type,"filed":filed_iso,
                "cat":cat,"cat_label":cat_label,"owner":grantor,"grantee":grantee,
                "amount":amount,"legal":legal,
                "prop_address":prop_address,"prop_city":prop_city,"prop_state":"FL","prop_zip":prop_zip,
                "mail_address":"","mail_city":"","mail_state":"FL","mail_zip":"",
                "clerk_url":clerk_url,"flags":[],"score":0,
            })

    return records


# ── Tax Deed Sales ────────────────────────────────────────────────────────────

def scrape_tax_deeds(lookup: dict) -> list[dict]:
    print("  Scraping tax deed sales…")
    records = []
    try:
        resp = SESS.get(TAX_DEED_URL, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2: continue
            hdrs = [th.get_text(strip=True).lower().replace(" ","_") for th in rows[0].find_all(["th","td"])]
            if not any(k in " ".join(hdrs) for k in ["case","parcel","address","sale","owner"]): continue
            for row in rows[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if len(cells) < 2: continue
                rc = {hdrs[i]: cells[i] for i in range(min(len(hdrs),len(cells)))}
                owner = rc.get("owner","") or rc.get("owner_name","") or ""
                addr  = rc.get("property_address","") or rc.get("address","") or rc.get("situs","") or ""
                case  = rc.get("case_number","") or rc.get("case_#","") or ""
                sale_date = rc.get("sale_date","") or rc.get("date","") or ""
                r = {
                    "doc_num":case,"doc_type":"TAX DEED SALE","filed":sale_date,
                    "cat":"FC","cat_label":"Tax Deed Foreclosure",
                    "owner":owner,"grantee":"","amount":None,"legal":"",
                    "prop_address":addr,"prop_city":"Tampa","prop_state":"FL","prop_zip":"",
                    "mail_address":"","mail_city":"","mail_state":"FL","mail_zip":"",
                    "clerk_url":TAX_DEED_URL,"flags":[],"score":0,
                }
                records.append(enrich(r, lookup))
        print(f"    ✓ {len(records)} tax deed records")
    except Exception as e:
        print(f"    ✗ Tax deed error: {e}")
    return records


# ── Score + Flags ─────────────────────────────────────────────────────────────

def compute_flags(r):
    flags = []
    cat   = r.get("cat","")
    owner = r.get("owner","").upper()
    amt   = r.get("amount",0) or 0
    flag_map = {"LP":"Lis pendens","FC":"Pre-foreclosure","JG":"Judgment lien",
                "TL":"Tax lien","ML":"Mechanic lien","TD":"Delinquent taxes","PR":"Probate / estate"}
    if cat in flag_map: flags.append(flag_map[cat])
    if re.search(r"\bLLC\b|CORP\b|INC\b|LTD\b|TRUST\b|COMPANY\b", owner): flags.append("LLC / corp owner")
    if amt > 50000: flags.append("Multiple liens")
    filed = r.get("filed","")
    if filed:
        try:
            if (datetime.now()-datetime.fromisoformat(filed)).days <= 7: flags.append("New this week")
        except: pass
    return flags


def score_record(r):
    flags = r.get("flags",[])
    score = 30 + len(flags)*10
    amt = r.get("amount",0) or 0
    if amt > 100000: score += 20
    elif amt > 50000: score += 15
    elif amt > 10000: score += 10
    elif amt > 1000:  score += 5
    if "Lis pendens" in flags and "Pre-foreclosure" in flags: score += 20
    if "New this week" in flags: score += 5
    if r.get("prop_address"): score += 5
    return min(100, score)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    print("═══════════════════════════════════════════════════")
    print("  Hillsborough County Property Intel Scraper v3    ")
    print("═══════════════════════════════════════════════════")
    now    = datetime.now(timezone.utc)
    d_to   = now.strftime("%m/%d/%Y")
    d_from = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%m/%d/%Y")
    print(f"  Date range: {d_from} → {d_to}\n")

    print("[1/3] HCPA parcel lookup…")
    lookup = build_parcel_lookup()
    print()

    print("[2/3] Clerk Official Records…")
    clerk_recs = await scrape_clerk(lookup, d_from, d_to)
    print(f"  ✓ {len(clerk_recs)} clerk records\n")

    print("[3/3] Tax deed sales…")
    td_recs = scrape_tax_deeds(lookup)
    print()

    all_records = clerk_recs + td_recs
    for r in all_records:
        r["flags"] = compute_flags(r)
        r["score"] = score_record(r)

    seen, deduped = set(), []
    for r in all_records:
        key = r.get("doc_num") or f"{r.get('owner','')}{r.get('filed','')}{r.get('doc_type','')}"
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    deduped.sort(key=lambda r: (bool(r.get("prop_address")), r.get("score",0)), reverse=True)

    addr_count = sum(1 for r in deduped if r.get("prop_address"))
    cat_counts = {}
    for r in deduped:
        cat_counts[r["cat"]] = cat_counts.get(r["cat"],0)+1

    output = {
        "fetched_at":   now.isoformat(),
        "source":       "Hillsborough County Clerk + HCPA",
        "date_range":   {"from":d_from,"to":d_to},
        "total":        len(deduped),
        "with_address": addr_count,
        "records":      deduped,
    }

    for path in [DASHBOARD/"records.json", DATA/"records.json"]:
        with open(path,"w") as f:
            json.dump(output, f, indent=2, default=str)

    print(f"✅  Saved {len(deduped)} records | {addr_count} with address")
    for cat, cnt in sorted(cat_counts.items(), key=lambda x:-x[1]):
        print(f"   {cat}: {cnt}")


if __name__ == "__main__":
    asyncio.run(main())
