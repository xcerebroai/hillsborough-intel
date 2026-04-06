"""
Hillsborough County Property Intelligence Scraper v4
====================================================
Fixes:
  1. HCPA: print actual CSV column names, use flexible matching
  2. Clerk: use JavaScript to force-set hidden select values,
     then trigger the search via direct URL construction using
     the known OBKey parameter pattern from the portal
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

TAX_DEED_URL = "https://www.hillsclerk.com/taxdeeds"
HCPA_DOWNLOADS = "https://downloads.hcpafl.org/"
LOOKBACK_DAYS = 3

# The Clerk portal doc-type search URL pattern discovered from network inspection:
# OBKey__1285_1 = doc type value  (e.g. "(LIS PEN) LIS PENDENS")
# OBKey__1634_1 = date from  (MM/DD/YYYY)
# OBKey__1634_2 = date to    (MM/DD/YYYY)
# CQID=319 = the "search by document type + date" query template
CLERK_SEARCH_URL = (
    "https://publicaccess.hillsclerk.com/oripublicaccess/"
    "?CQID=319"
    "&OBKey__1285_1={doctype}"
    "&OBKey__1634_1={datefrom}"
    "&OBKey__1634_2={dateto}"
)

SESS = requests.Session()
SESS.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://publicaccess.hillsclerk.com/",
})

DOC_TYPE_MAP = {
    "LIS PEN":        ("LP", "Lis Pendens"),
    "LIS PENDENS":    ("LP", "Lis Pendens"),
    "NOFC":           ("FC", "Foreclosure"),
    "FORECLOSURE":    ("FC", "Foreclosure"),
    "CERT TITLE":     ("FC", "Foreclosure"),
    "CERT SALE":      ("FC", "Foreclosure"),
    "TAX DEED":       ("FC", "Tax Deed Foreclosure"),
    "JUDGMENT":       ("JG", "Judgment"),
    "JDG":            ("JG", "Judgment"),
    "FINAL JDG":      ("JG", "Judgment"),
    "CERT JUDGMENT":  ("JG", "Judgment"),
    "TAX LIEN":       ("TL", "Tax Lien"),
    "IRS LIEN":       ("TL", "IRS/Federal Tax Lien"),
    "FED TAX":        ("TL", "Federal Tax Lien"),
    "FEDERAL TAX":    ("TL", "Federal Tax Lien"),
    "MECH LIEN":      ("ML", "Mechanic's Lien"),
    "MECHANIC":       ("ML", "Mechanic's Lien"),
    "CLAIM OF LIEN":  ("ML", "Mechanic's Lien"),
    "HOA LIEN":       ("ML", "HOA Lien"),
    "LIEN":           ("ML", "Lien"),
    "PROBATE":        ("PR", "Probate"),
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
        print(f"    ✗ HCPA file list error: {e}")
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
        resp = SESS.post(HCPA_DOWNLOADS, data=data, timeout=180, stream=True)
        resp.raise_for_status()
        content = b"".join(resp.iter_content(1<<20))
        print(f"    Downloaded {len(content)//1024}KB")
        return content
    except Exception as e:
        print(f"    ✗ POST error: {e}")
        return None


def parse_zip_csv(content, label):
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            csv_names = [n for n in z.namelist() if n.lower().endswith((".csv",".txt"))]
            if not csv_names:
                print(f"    ✗ No CSV in {label}")
                return []
            with z.open(csv_names[0]) as f:
                text = f.read().decode("latin-1")
                rows = list(csv.DictReader(io.StringIO(text)))
                if rows:
                    print(f"    Columns: {list(rows[0].keys())[:12]}")
                return rows
    except Exception as e:
        print(f"    ✗ ZIP parse error: {e}")
        return []


def normalize_name(name):
    name = name.upper().strip()
    for sfx in [" JR"," SR"," II"," III"," IV"," TRUST"," TR"," ET AL"," ET UX"," ETAL"]:
        name = name.replace(sfx,"")
    name = re.sub(r"[^A-Z ]","",name)
    return re.sub(r"\s+"," ",name).strip()


def build_parcel_lookup():
    print("  Fetching HCPA file list…")
    files, soup = hcpa_get_files()
    if not files:
        return {"by_owner":{},"by_folio":{}}

    print(f"  Found {len(files)} files")
    by_owner, by_folio = {}, {}
    rows = []

    # Try LatLon first
    for name, target in files.items():
        if re.search(r"LatLon_Table", name, re.I):
            print(f"  Downloading {name}…")
            content = hcpa_post_download(target, soup)
            if content and content[:2] == b'PK':
                rows = parse_zip_csv(content, name)
                print(f"  ✓ {len(rows):,} rows from LatLon")
            break

    if not rows:
        for name, target in files.items():
            if re.search(r"allsales", name, re.I):
                print(f"  Fallback: {name}…")
                content = hcpa_post_download(target, soup)
                if content and content[:2] == b'PK':
                    rows = parse_zip_csv(content, name)
                    print(f"  ✓ {len(rows):,} rows from allsales")
                break

    if not rows:
        print("  ✗ No parcel data loaded")
        return {"by_owner":{},"by_folio":{}}

    # Build lookup using flexible column matching
    for row in rows:
        # Normalize all keys to uppercase no-space
        norm = {}
        for k, v in row.items():
            clean_key = re.sub(r"[^A-Z0-9]","",k.upper().strip())
            norm[clean_key] = (v or "").strip()

        def get(*keys):
            for k in keys:
                k = re.sub(r"[^A-Z0-9]","",k.upper())
                if norm.get(k): return norm[k]
            return ""

        folio = get("FOLIO","PARCELID","FOLIOID","FOLIONO","ACCOUNT","PARCELNUMBER")
        owner = get("OWN1","OWNER1","OWNERNAME","OWNER","GRANTOR","GRANTORNAME")
        addr  = get("SITEADDR","SITEADDRESS","PROPADDR","PROPERTYADDRESS","ADDRESS","PHYSADDR","SITUS","SITUSADDRESS")
        city  = get("SITECITY","PROPCITY","PROPERTYCITY","CITY","SITUSCITY") or "Tampa"
        zip_  = get("SITEZIP","PROPZIP","PROPERTYZIP","ZIP","ZIPCODE","SITUSZIP")
        mail_addr  = get("MAILADR1","MAILADDR1","MAILADDRESS","MAILINGADDRESS") or addr
        mail_city  = get("MAILCITY","MAILINGCITY") or city
        mail_state = get("MAILSTATE","MAILINGSTATE") or "FL"
        mail_zip   = get("MAILZIP","MAILINGZIP") or zip_

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


# ── Clerk Scraper — JavaScript force-set approach ─────────────────────────────

async def scrape_clerk(lookup, date_from, date_to):
    records = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=SESS.headers["User-Agent"],
            viewport={"width":1280,"height":900},
            extra_http_headers={"Referer":"https://publicaccess.hillsclerk.com/"}
        )
        page = await context.new_page()

        # Step 1: Load page and get all doc type options from the hidden select
        print("  Loading Clerk portal to get doc type options…")
        try:
            await page.goto("https://publicaccess.hillsclerk.com/oripublicaccess/",
                           wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            # Get all options from OBKey__1285_1 (the doc type select)
            all_options = await page.evaluate("""() => {
                const sel = document.querySelector('#OBKey__1285_1, [name="OBKey__1285_1"]');
                if (!sel) return [];
                return Array.from(sel.options).map(o => ({val: o.value, txt: o.text.trim()}));
            }""")

            print(f"  Found {len(all_options)} doc type options")

            # Find options matching our target doc types
            target_keywords = {
                "LIS PEN": ("LP","Lis Pendens"),
                "LISPEN":  ("LP","Lis Pendens"),
                "NOFC":    ("FC","Foreclosure"),
                "JUDGMENT":("JG","Judgment"),
                "JDG":     ("JG","Judgment"),
                "MECH":    ("ML","Mechanic's Lien"),
                "CLAIM":   ("ML","Mechanic's Lien"),
                "IRS":     ("TL","IRS/Federal Tax Lien"),
                "TAX LIEN":("TL","Tax Lien"),
                "HOA":     ("ML","HOA Lien"),
                "PROBATE": ("PR","Probate"),
                "FORECLOS":("FC","Foreclosure"),
            }

            matched_options = []
            for opt in all_options:
                txt_upper = opt["txt"].upper()
                val_upper = opt["val"].upper()
                for kw, cat_info in target_keywords.items():
                    if kw in txt_upper or kw in val_upper:
                        matched_options.append({
                            "val": opt["val"],
                            "txt": opt["txt"],
                            "cat": cat_info[0],
                            "cat_label": cat_info[1]
                        })
                        break

            print(f"  Matched {len(matched_options)} target doc types:")
            for o in matched_options:
                print(f"    {o['val']!r} → {o['cat']} ({o['cat_label']})")

        except Exception as e:
            print(f"  ✗ Could not load portal or get options: {e}")
            await browser.close()
            return []

        # Step 2: For each matched doc type, use JavaScript to fill and submit
        for opt in matched_options:
            print(f"\n  → Searching: {opt['txt']}")
            try:
                await page.goto("https://publicaccess.hillsclerk.com/oripublicaccess/",
                               wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)

                # Use JavaScript to:
                # 1. Click the "Document Type" search tab if it exists
                # 2. Force-set the select value
                # 3. Fill date fields
                # 4. Submit
                result = await page.evaluate(f"""() => {{
                    try {{
                        // Try to activate the doc type search tab
                        const tabs = document.querySelectorAll('a, button, li, div[role="tab"]');
                        for (const t of tabs) {{
                            const txt = (t.textContent || '').toLowerCase();
                            if (txt.includes('document type') || txt.includes('doc type') || txt.includes('instrument type')) {{
                                t.click();
                                break;
                            }}
                        }}

                        // Force-set the doc type select
                        const docSel = document.querySelector('#OBKey__1285_1, [name="OBKey__1285_1"]');
                        if (docSel) {{
                            docSel.value = {json.dumps(opt['val'])};
                            docSel.dispatchEvent(new Event('change', {{bubbles:true}}));
                        }}

                        // Fill date from
                        const dateFrom = document.querySelector('#OBKey__1634_1, [name="OBKey__1634_1"]');
                        if (dateFrom) {{
                            dateFrom.value = {json.dumps(date_from)};
                            dateFrom.dispatchEvent(new Event('change', {{bubbles:true}}));
                        }}

                        // Fill date to
                        const dateTo = document.querySelector('#OBKey__1634_2, [name="OBKey__1634_2"]');
                        if (dateTo) {{
                            dateTo.value = {json.dumps(date_to)};
                            dateTo.dispatchEvent(new Event('change', {{bubbles:true}}));
                        }}

                        // Find and click search button
                        const buttons = document.querySelectorAll('input[type=submit], button[type=submit], input[value*="Search"], button');
                        for (const b of buttons) {{
                            const txt = (b.value || b.textContent || '').toLowerCase();
                            if (txt.includes('search') || txt.includes('submit') || txt.includes('find')) {{
                                b.click();
                                return 'submitted via ' + (b.value || b.textContent);
                            }}
                        }}

                        // Fallback: submit the form directly
                        const form = document.querySelector('form');
                        if (form) {{
                            form.submit();
                            return 'form submitted';
                        }}

                        return 'no submit found';
                    }} catch(e) {{
                        return 'error: ' + e.message;
                    }}
                }}""")

                print(f"    JS result: {result}")
                await page.wait_for_load_state("networkidle", timeout=20000)
                await page.wait_for_timeout(2000)

                recs = await parse_results(page, opt)
                print(f"    ✓ {len(recs)} records")
                records.extend(recs)

            except Exception as e:
                print(f"    ✗ Error: {e}")

            await asyncio.sleep(1)

        await browser.close()

    # Enrich addresses
    enriched = 0
    for r in records:
        before = bool(r.get("prop_address"))
        r = enrich(r, lookup)
        if r.get("prop_address") and not before:
            enriched += 1
    print(f"\n  Address enrichment: {enriched}/{len(records)} records")
    return records


async def parse_results(page, opt):
    records = []
    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        headers = [th.get_text(strip=True).lower().replace(" ","_").replace("#","num")
                   for th in rows[0].find_all(["th","td"])]
        hdr_str = " ".join(headers)
        if not any(k in hdr_str for k in ["instrument","grantor","grantee","doc","instr","book","recorded"]):
            continue

        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cells) < 2 or not any(cells):
                continue
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
            dt_upper = doc_type.upper()
            for key, val in DOC_TYPE_MAP.items():
                if key in dt_upper:
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
                owner = rc.get("owner","") or rc.get("owner_name","") or ""
                addr  = rc.get("property_address","") or rc.get("address","") or ""
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
    print("  Hillsborough County Property Intel Scraper v4    ")
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
