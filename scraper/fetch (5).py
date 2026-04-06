"""
Hillsborough County Property Intel Scraper v6
====================================================
Fixes:
  1. Use HCparcel_4_public zip instead of LatLon — has full address+owner data
  2. Print ALL 64 Clerk options so we can see exact names for missing doc types
  3. Keep expanded keyword matching
"""

import asyncio
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

try:
    from dbfread import DBF
    HAS_DBF = True
except ImportError:
    HAS_DBF = False

ROOT      = Path(__file__).parent.parent
DASHBOARD = ROOT / "dashboard"
DATA      = ROOT / "data"
DASHBOARD.mkdir(exist_ok=True)
DATA.mkdir(exist_ok=True)

TAX_DEED_URL   = "https://www.hillsclerk.com/taxdeeds"
HCPA_DOWNLOADS = "https://downloads.hcpafl.org/"
LOOKBACK_DAYS  = 3

SESS = requests.Session()
SESS.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
})

DOC_TYPE_MAP = {
    "LIS PEN":       ("LP", "Lis Pendens"),
    "LIS PENDENS":   ("LP", "Lis Pendens"),
    "NOFC":          ("FC", "Foreclosure"),
    "FORECLOS":      ("FC", "Foreclosure"),
    "CERT TITLE":    ("FC", "Foreclosure"),
    "TAX DEED":      ("FC", "Tax Deed"),
    "JUDGMENT":      ("JG", "Judgment"),
    "JDG":           ("JG", "Judgment"),
    "CERT JUDGMENT": ("JG", "Judgment"),
    "TAX LIEN":      ("TL", "Tax Lien"),
    "IRS LIEN":      ("TL", "IRS/Federal Tax Lien"),
    "FED TAX":       ("TL", "Federal Tax Lien"),
    "CORP TAX":      ("TL", "Corp Tax Lien"),
    "MECH LIEN":     ("ML", "Mechanic's Lien"),
    "CLAIM OF LIEN": ("ML", "Mechanic's Lien"),
    "HOA LIEN":      ("ML", "HOA Lien"),
    "LIEN":          ("ML", "Lien"),
    "PROBATE":       ("PR", "Probate"),
}

TARGET_KEYWORDS = {
    # Lis Pendens
    "LIS PEN":   ("LP", "Lis Pendens"),
    "LISPEN":    ("LP", "Lis Pendens"),
    "LP":        ("LP", "Lis Pendens"),
    # Foreclosure
    "NOFC":      ("FC", "Foreclosure"),
    "FORECLOS":  ("FC", "Foreclosure"),
    "TAXDEED":   ("FC", "Tax Deed"),
    "TAX DEED":  ("FC", "Tax Deed"),
    "CERTSALE":  ("FC", "Cert of Sale"),
    "CERT SALE": ("FC", "Cert of Sale"),
    # Judgments
    "JUD":       ("JG", "Judgment"),
    "JUDGMENT":  ("JG", "Judgment"),
    "CCJ":       ("JG", "Certified Court Judgment"),
    "DRJUD":     ("JG", "Domestic Relations Judgment"),
    # Tax Liens
    "LNCORPTX":  ("TL", "Corp Tax Lien"),
    "LNIRS":     ("TL", "IRS Lien"),
    "IRS":       ("TL", "IRS/Federal Tax Lien"),
    "LNFED":     ("TL", "Federal Tax Lien"),
    "LNSTATE":   ("TL", "State Tax Lien"),
    "LNTAX":     ("TL", "Tax Lien"),
    "TAX LIEN":  ("TL", "Tax Lien"),
    # Mechanic / HOA Liens
    "LNMECH":    ("ML", "Mechanic's Lien"),
    "MECH":      ("ML", "Mechanic's Lien"),
    "CLAIM":     ("ML", "Claim of Lien"),
    "LNHOA":     ("ML", "HOA Lien"),
    "HOA":       ("ML", "HOA Lien"),
    "LNCONDO":   ("ML", "Condo Lien"),
    "CONDO":     ("ML", "Condo Lien"),
    "CND":       ("ML", "Condo Declaration"),
    # Probate
    "PRO":       ("PR", "Probate"),
    "PROBATE":   ("PR", "Probate"),
}


# ── HCPA Bulk Download ────────────────────────────────────────────────────────

def hcpa_get_files():
    try:
        resp = SESS.get(HCPA_DOWNLOADS, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")
        files = {}
        for row in soup.select("table tr"):
            cells = row.find_all("td")
            if len(cells) >= 2:
                name = cells[0].get_text(strip=True)
                link = cells[0].find("a")
                if link and "__doPostBack" in (link.get("href","") or ""):
                    m = re.search(r"__doPostBack\('([^']+)'", link["href"])
                    if m:
                        files[name] = m.group(1)
        return files, soup
    except Exception as e:
        print(f"    ✗ File list error: {e}")
        return {}, None


def hcpa_post_download(target_arg, soup):
    try:
        data = {
            "__EVENTTARGET": target_arg,
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": (soup.find("input",{"name":"__VIEWSTATE"}) or {}).get("value",""),
            "__VIEWSTATEGENERATOR": (soup.find("input",{"name":"__VIEWSTATEGENERATOR"}) or {}).get("value",""),
            "__EVENTVALIDATION": (soup.find("input",{"name":"__EVENTVALIDATION"}) or {}).get("value",""),
        }
        resp = SESS.post(HCPA_DOWNLOADS, data=data, timeout=300, stream=True)
        resp.raise_for_status()
        content = b"".join(resp.iter_content(1<<20))
        print(f"    Downloaded {len(content)//1024}KB")
        return content
    except Exception as e:
        print(f"    ✗ POST error: {e}")
        return None


def parse_dbf_from_zip(content, label):
    """Extract and parse DBF file from zip content."""
    rows = []
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            names = z.namelist()
            print(f"    Files in zip: {names}")
            dbf_files = [n for n in names if n.lower().endswith(".dbf")]
            if not dbf_files:
                print(f"    No DBF files found")
                return []
            if not HAS_DBF:
                print(f"    dbfread not available")
                return []

            fname = dbf_files[0]
            print(f"    Reading {fname}…")
            with z.open(fname) as f:
                dbf_bytes = f.read()

            import tempfile, os
            with tempfile.NamedTemporaryFile(suffix=".dbf", delete=False) as tmp:
                tmp.write(dbf_bytes)
                tmp_path = tmp.name
            try:
                table = DBF(tmp_path, encoding="latin-1", ignore_missing_memofile=True)
                rows = [dict(r) for r in table]
                if rows:
                    print(f"    Columns: {list(rows[0].keys())}")
                    print(f"    Sample row: { {k: v for k, v in list(rows[0].items())[:8]} }")
            finally:
                os.unlink(tmp_path)
    except Exception as e:
        print(f"    ✗ DBF parse error: {e}")
    return rows


def normalize_name(name):
    name = str(name).upper().strip()
    for sfx in [" JR"," SR"," II"," III"," IV"," TRUST"," TR"," ET AL"," ET UX"," ETAL"]:
        name = name.replace(sfx,"")
    name = re.sub(r"[^A-Z ]","",name)
    return re.sub(r"\s+"," ",name).strip()


def build_parcel_lookup():
    print("  Fetching HCPA file list…")
    files, soup = hcpa_get_files()
    if not files:
        return {"by_owner":{},"by_folio":{}}

    print(f"  Found {len(files)} files:")
    for name in files:
        print(f"    {name}")

    by_owner, by_folio = {}, {}
    rows = []

    # Priority order: parcel_4_public has full owner+address data
    priority = ["HCparcel_4_public", "parcel_", "LatLon_Table", "allsales"]

    for priority_key in priority:
        for name, target in files.items():
            if re.search(priority_key, name, re.I):
                print(f"\n  Downloading {name}…")
                content = hcpa_post_download(target, soup)
                if content and content[:2] == b'PK':
                    rows = parse_dbf_from_zip(content, name)
                    if rows:
                        print(f"  ✓ {len(rows):,} rows loaded")
                        break
        if rows:
            break

    if not rows:
        print("  ✗ No parcel data loaded")
        return {"by_owner":{},"by_folio":{}}

    # Build lookup with flexible column matching
    for row in rows:
        norm = {}
        for k, v in row.items():
            ck = re.sub(r"[^A-Z0-9]", "", str(k).upper().strip())
            norm[ck] = str(v or "").strip()

        def get(*keys):
            for k in keys:
                ck = re.sub(r"[^A-Z0-9]","",k.upper())
                if norm.get(ck) and norm[ck] not in ["None","NULL",""]:
                    return norm[ck]
            return ""

        folio = get("FOLIO","PARCELID","FOLIOID","FOLIONO","ACCOUNT","PARCELNUMBER","FOLIO1")
        owner = get("OWN1","OWNER1","OWNERNAME","OWNER","GRANTOR","OWN2")
        addr  = get("SITEADDR","SITEADDRESS","PROPADDR","PROPERTYADDRESS","ADDRESS",
                    "PHYSADDR","SITUS","SITUSADDRESS","SITEADR","PHY1","PHYSICALADDR")
        city  = get("SITECITY","PROPCITY","CITY","SITUSCITY","SITECIT") or "Tampa"
        zip_  = get("SITEZIP","PROPZIP","ZIP","ZIPCODE","SITEZIP5","SITUSZIP")
        mail1 = get("MAILADR1","MAILADDR1","MAILADDRESS","MAILINGADDRESS","MAIL1","MAILADD1")
        mail2 = get("MAILADR2","MAILADDR2","MAIL2","MAILADD2")
        mail_addr  = (mail1 + " " + mail2).strip() or addr
        mail_city  = get("MAILCITY","MAILINGCITY","MAILCIT") or city
        mail_state = get("MAILSTATE","MAILINGSTATE","MAILST") or "FL"
        mail_zip   = get("MAILZIP","MAILINGZIP","MAILZIP5") or zip_

        info = {
            "prop_address": addr,
            "prop_city":    city or "Tampa",
            "prop_state":   "FL",
            "prop_zip":     zip_,
            "mail_address": mail_addr,
            "mail_city":    mail_city or "Tampa",
            "mail_state":   mail_state or "FL",
            "mail_zip":     mail_zip or zip_,
        }

        if folio:
            by_folio[folio.strip()] = info
        if owner:
            key = normalize_name(owner)
            if len(key) > 3:
                by_owner[key] = info

    print(f"  ✓ {len(by_owner):,} owners | {len(by_folio):,} folios")
    return {"by_owner": by_owner, "by_folio": by_folio}


def enrich(record, lookup):
    if record.get("prop_address"):
        return record
    key = normalize_name(record.get("owner",""))
    if key and key in lookup["by_owner"]:
        info = lookup["by_owner"][key]
        for k, v in info.items():
            if not record.get(k):
                record[k] = v
        return record
    legal = record.get("legal","")
    if legal:
        am = re.search(r"(\d{2,5}\s+[A-Z][A-Z0-9\s]{2,25}?(?:ST|AVE|BLVD|DR|RD|LN|WAY|CT|CIR|PL|TER|TRL|PKWY)\b)", legal.upper())
        if am: record["prop_address"] = am.group(1).title()
        cm = re.search(r"\b(TAMPA|BRANDON|RIVERVIEW|PLANT CITY|VALRICO|SEFFNER|LUTZ|WESLEY CHAPEL|RUSKIN|SUN CITY CENTER|GIBSONTON|WIMAUMA|TEMPLE TERRACE)\b", legal.upper())
        if cm: record["prop_city"] = cm.group(1).title()
        zm = re.search(r"\b(336\d{2})\b", legal)
        if zm: record["prop_zip"] = zm.group(1)
    return record


# ── Clerk Scraper ─────────────────────────────────────────────────────────────

async def scrape_clerk(lookup, date_from, date_to):
    records = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=SESS.headers["User-Agent"],
            viewport={"width":1280,"height":900},
        )
        page = await context.new_page()

        print("  Loading Clerk portal…")
        try:
            await page.goto("https://publicaccess.hillsclerk.com/oripublicaccess/",
                           wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            all_options = await page.evaluate("""() => {
                const sel = document.querySelector('#OBKey__1285_1, [name="OBKey__1285_1"]');
                if (!sel) return [];
                return Array.from(sel.options).map(o => ({val: o.value, txt: o.text.trim()}));
            }""")

            print(f"  All {len(all_options)} doc type options:")
            for o in all_options:
                if o["val"]:
                    print(f"    {o['val']!r}")

            matched = []
            seen_vals = set()
            for opt in all_options:
                if not opt["val"]: continue
                combined = (opt["txt"] + " " + opt["val"]).upper()
                for kw, cat_info in TARGET_KEYWORDS.items():
                    if kw in combined and opt["val"] not in seen_vals:
                        matched.append({
                            "val": opt["val"],
                            "txt": opt["txt"],
                            "cat": cat_info[0],
                            "cat_label": cat_info[1]
                        })
                        seen_vals.add(opt["val"])
                        break

            print(f"\n  Matched {len(matched)} doc types")

        except Exception as e:
            print(f"  ✗ Portal load error: {e}")
            await browser.close()
            return []

        for opt in matched:
            print(f"\n  → {opt['txt']}")
            try:
                await page.goto("https://publicaccess.hillsclerk.com/oripublicaccess/",
                               wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(1500)

                result = await page.evaluate(f"""() => {{
                    try {{
                        const docSel = document.querySelector('#OBKey__1285_1, [name="OBKey__1285_1"]');
                        if (docSel) {{
                            docSel.value = {json.dumps(opt['val'])};
                            docSel.dispatchEvent(new Event('change', {{bubbles:true}}));
                        }}
                        const df = document.querySelector('#OBKey__1634_1, [name="OBKey__1634_1"]');
                        if (df) {{ df.value = {json.dumps(date_from)}; df.dispatchEvent(new Event('change',{{bubbles:true}})); }}
                        const dt = document.querySelector('#OBKey__1634_2, [name="OBKey__1634_2"]');
                        if (dt) {{ dt.value = {json.dumps(date_to)}; dt.dispatchEvent(new Event('change',{{bubbles:true}})); }}
                        const buttons = document.querySelectorAll('input[type=submit],button[type=submit],input[value*="Search"],button');
                        for (const b of buttons) {{
                            const t = (b.value||b.textContent||'').toLowerCase();
                            if (t.includes('search')||t.includes('submit')||t.includes('find')) {{
                                b.click();
                                return 'ok: '+(b.value||b.textContent).trim();
                            }}
                        }}
                        const form = document.querySelector('form');
                        if (form) {{ form.submit(); return 'form.submit'; }}
                        return 'no submit';
                    }} catch(e) {{ return 'err:'+e.message; }}
                }}""")

                await page.wait_for_load_state("networkidle", timeout=20000)
                await page.wait_for_timeout(1500)

                recs = await parse_results(page, opt)
                print(f"    ✓ {len(recs)} records")
                records.extend(recs)

            except Exception as e:
                print(f"    ✗ {e}")
            await asyncio.sleep(0.8)

        await browser.close()

    enriched = 0
    for r in records:
        before = bool(r.get("prop_address"))
        r = enrich(r, lookup)
        if r.get("prop_address") and not before:
            enriched += 1
    print(f"\n  Enriched {enriched}/{len(records)} records with addresses")
    return records


async def parse_results(page, opt):
    records = []
    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2: continue
        headers = [th.get_text(strip=True).lower().replace(" ","_").replace("#","num")
                   for th in rows[0].find_all(["th","td"])]
        if not any(k in " ".join(headers) for k in ["instrument","grantor","grantee","doc","instr","book","recorded"]):
            continue

        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cells) < 2 or not any(cells): continue
            cm = {headers[i]: cells[i] for i in range(min(len(headers),len(cells)))}

            def get(*keys):
                for k in keys:
                    for h, v in cm.items():
                        if k in h and v and v not in ["-","—",""]:
                            return v
                return ""

            doc_num  = get("instrument","instr","doc_num","book")
            doc_type = get("type","doc_type","doc") or opt["txt"]
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
                    if v > 0: amount = v
                except: pass

            filed_iso = ""
            for fmt in ["%m/%d/%Y","%Y-%m-%d","%m-%d-%Y","%m/%d/%y"]:
                try:
                    filed_iso = datetime.strptime(filed.strip(), fmt).date().isoformat()
                    break
                except: pass

            cat, cat_label = opt["cat"], opt["cat_label"]
            for key, val in DOC_TYPE_MAP.items():
                if key in doc_type.upper():
                    cat, cat_label = val
                    break

            prop_address, prop_city, prop_zip = "","",""
            if legal:
                am = re.search(r"(\d{2,5}\s+[A-Z][A-Z0-9\s]{2,25}?(?:ST|AVE|BLVD|DR|RD|LN|WAY|CT|CIR|PL|TER|TRL|PKWY)\b)", legal.upper())
                if am: prop_address = am.group(1).title()
                cm2 = re.search(r"\b(TAMPA|BRANDON|RIVERVIEW|PLANT CITY|VALRICO|SEFFNER|LUTZ|WESLEY CHAPEL|RUSKIN|SUN CITY CENTER|GIBSONTON|WIMAUMA|TEMPLE TERRACE)\b", legal.upper())
                if cm2: prop_city = cm2.group(1).title()
                zm = re.search(r"\b(336\d{2})\b", legal)
                if zm: prop_zip = zm.group(1)

            num_clean = re.sub(r"[^0-9]","",doc_num)
            clerk_url = f"https://publicaccess.hillsclerk.com/oripublicaccess/?instrument={num_clean}" if num_clean else "https://publicaccess.hillsclerk.com/oripublicaccess/"

            records.append({
                "doc_num":doc_num,"doc_type":doc_type,"filed":filed_iso,
                "cat":cat,"cat_label":cat_label,"owner":grantor,"grantee":grantee,
                "amount":amount,"legal":legal,
                "prop_address":prop_address,"prop_city":prop_city,"prop_state":"FL","prop_zip":prop_zip,
                "mail_address":"","mail_city":"","mail_state":"FL","mail_zip":"",
                "clerk_url":clerk_url,"flags":[],"score":0,
            })
    return records


# ── Tax Deed Sales ─────────────────────────────────────────────────────────────

def scrape_tax_deeds(lookup):
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
                owner     = rc.get("owner","") or rc.get("owner_name","") or ""
                addr      = rc.get("property_address","") or rc.get("address","") or ""
                case      = rc.get("case_number","") or rc.get("case_#","") or ""
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
        print(f"    ✓ {len(records)} records")
    except Exception as e:
        print(f"    ✗ {e}")
    return records


# ── Score + Flags ──────────────────────────────────────────────────────────────

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


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    print("═══════════════════════════════════════════════════")
    print("  Hillsborough County Property Intel Scraper v6    ")
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

    deduped.sort(key=lambda r:(bool(r.get("prop_address")), r.get("score",0)), reverse=True)

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
