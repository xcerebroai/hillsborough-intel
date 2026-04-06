"""
Hillsborough County Property Intelligence Scraper v2
=====================================================
Strategy:
  1. Download HCPA LatLon + Parcel bulk data → build owner/address lookup table
  2. Scrape Hillsborough Clerk Official Records via Playwright
  3. Cross-reference records by owner name / legal description to fill addresses
  4. Output dashboard/records.json
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

ROOT      = Path(__file__).parent.parent
DASHBOARD = ROOT / "dashboard"
DATA      = ROOT / "data"
DASHBOARD.mkdir(exist_ok=True)
DATA.mkdir(exist_ok=True)

HCPA_DOWNLOADS = "https://downloads.hcpafl.org/"
CLERK_SEARCH   = "https://publicaccess.hillsclerk.com/oripublicaccess/"
TAX_DEED_URL   = "https://www.hillsclerk.com/taxdeeds"
LOOKBACK_DAYS  = 2

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}

DOC_TYPE_MAP = {
    "LIS PEN":       ("LP", "Lis Pendens"),
    "LIS PENDENS":   ("LP", "Lis Pendens"),
    "FORECLOSURE":   ("FC", "Foreclosure"),
    "NOFC":          ("FC", "Foreclosure"),
    "CERT TITLE":    ("FC", "Foreclosure"),
    "CERT SALE":     ("FC", "Foreclosure"),
    "JUDGMENT":      ("JG", "Judgment"),
    "JDG":           ("JG", "Judgment"),
    "FINAL JDG":     ("JG", "Judgment"),
    "CERT JUDGMENT": ("JG", "Judgment"),
    "TAX LIEN":      ("TL", "Tax Lien"),
    "IRS LIEN":      ("TL", "IRS/Federal Tax Lien"),
    "FED TAX":       ("TL", "Federal Tax Lien"),
    "FEDERAL TAX":   ("TL", "Federal Tax Lien"),
    "STATE TAX":     ("TL", "State Tax Lien"),
    "MECH LIEN":     ("ML", "Mechanic's Lien"),
    "CLAIM OF LIEN": ("ML", "Mechanic's Lien"),
    "HOA LIEN":      ("ML", "HOA Lien"),
    "LIEN":          ("ML", "Lien"),
    "PROBATE":       ("PR", "Probate"),
    "NOTICE ADMIN":  ("PR", "Probate / Administration"),
}


# ── HCPA Bulk Lookup ──────────────────────────────────────────────────────────

def get_latest_filename(pattern):
    try:
        resp = requests.get(HCPA_DOWNLOADS, timeout=30, headers=HEADERS)
        soup = BeautifulSoup(resp.text, "html.parser")
        for row in soup.select("table tr"):
            cells = row.find_all("td")
            if cells:
                name = cells[0].get_text(strip=True)
                if re.search(pattern, name, re.IGNORECASE):
                    return name
    except Exception as e:
        print(f"    ✗ Downloads page error: {e}")
    return None


def download_csv_from_zip(filename):
    url = HCPA_DOWNLOADS + filename
    print(f"    Downloading {filename}…")
    try:
        resp = requests.get(url, timeout=120, headers=HEADERS, stream=True)
        resp.raise_for_status()
        content = b"".join(resp.iter_content(1 << 20))
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            csv_files = [n for n in z.namelist() if n.lower().endswith((".csv", ".txt"))]
            if not csv_files:
                return []
            with z.open(csv_files[0]) as f:
                import csv
                text = f.read().decode("latin-1")
                return list(csv.DictReader(io.StringIO(text)))
    except Exception as e:
        print(f"    ✗ Download error: {e}")
        return []


def normalize_name(name):
    name = name.upper().strip()
    for sfx in [" JR", " SR", " II", " III", " IV", " TRUST", " TR", " ET AL", " ET UX"]:
        name = name.replace(sfx, "")
    name = re.sub(r"[^A-Z ]", "", name)
    return re.sub(r"\s+", " ", name).strip()


def build_parcel_lookup():
    print("  Building HCPA parcel lookup…")
    by_owner = {}
    by_folio = {}

    filename = get_latest_filename(r"LatLon_Table.*\.zip")
    rows = download_csv_from_zip(filename) if filename else []

    if not rows:
        # fallback: try parcel zip (larger but more data)
        filename = get_latest_filename(r"parcel_.*\.zip")
        rows = download_csv_from_zip(filename) if filename else []

    print(f"    ✓ {len(rows):,} parcel rows loaded")

    for row in rows:
        norm = {k.strip().upper().replace(" ", "_"): (v or "").strip() for k, v in row.items()}

        folio = norm.get("FOLIO") or norm.get("PARCEL_ID") or norm.get("FOLIO_NO") or ""
        owner = norm.get("OWN1") or norm.get("OWNER1") or norm.get("OWNER_NAME") or norm.get("OWNER") or ""
        addr  = norm.get("SITEADDR") or norm.get("SITE_ADDR") or norm.get("PROP_ADDR") or norm.get("ADDRESS") or norm.get("PHY_ADDR") or ""
        city  = norm.get("SITECITY") or norm.get("SITE_CITY") or norm.get("PROP_CITY") or norm.get("CITY") or "Tampa"
        zip_  = norm.get("SITEZIP") or norm.get("SITE_ZIP") or norm.get("PROP_ZIP") or norm.get("ZIP") or ""
        mail_addr  = norm.get("MAILADR1") or norm.get("MAIL_ADDR1") or norm.get("MAIL_ADDRESS") or addr
        mail_city  = norm.get("MAILCITY") or norm.get("MAIL_CITY") or city
        mail_state = norm.get("MAILSTATE") or norm.get("MAIL_STATE") or "FL"
        mail_zip   = norm.get("MAILZIP") or norm.get("MAIL_ZIP") or zip_

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
            by_folio[folio.strip()] = info
        if owner:
            key = normalize_name(owner)
            if len(key) > 3:
                by_owner[key] = info

    return {"by_owner": by_owner, "by_folio": by_folio}


def enrich_from_lookup(record, lookup):
    if record.get("prop_address"):
        return record

    # Try owner name
    owner_key = normalize_name(record.get("owner", ""))
    if owner_key and owner_key in lookup["by_owner"]:
        info = lookup["by_owner"][owner_key]
        record.update({k: v for k, v in info.items() if not record.get(k)})
        return record

    # Try extracting from legal description
    legal = record.get("legal", "")
    if legal:
        addr_m = re.search(
            r"(\d{2,5}\s+[A-Z][A-Z0-9\s]{3,30}?(?:ST|AVE|BLVD|DR|RD|LN|WAY|CT|CIR|PL|TER|TRL|PKWY)\b)",
            legal.upper()
        )
        if addr_m:
            record["prop_address"] = addr_m.group(1).title()
        city_m = re.search(
            r"\b(TAMPA|BRANDON|RIVERVIEW|PLANT CITY|VALRICO|SEFFNER|LUTZ|WESLEY CHAPEL|RUSKIN|SUN CITY CENTER|GIBSONTON|WIMAUMA|TEMPLE TERRACE)\b",
            legal.upper()
        )
        if city_m:
            record["prop_city"] = city_m.group(1).title()
        zip_m = re.search(r"\b(336\d{2})\b", legal)
        if zip_m:
            record["prop_zip"] = zip_m.group(1)

    return record


# ── Clerk Scraper ─────────────────────────────────────────────────────────────

async def scrape_clerk(lookup, date_from, date_to):
    records = []
    doc_codes = [
        "LIS PEN", "NOFC", "JUDGMENT", "MECH LIEN",
        "CLAIM OF LIEN", "IRS LIEN", "TAX LIEN", "HOA LIEN", "PROBATE",
    ]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        for code in doc_codes:
            print(f"  → Clerk: {code}")
            recs = await search_doc_type(page, code, date_from, date_to)
            print(f"    ✓ {len(recs)} records")
            records.extend(recs)
            await asyncio.sleep(1)

        await browser.close()

    enriched = 0
    for r in records:
        before = r.get("prop_address", "")
        r = enrich_from_lookup(r, lookup)
        if r.get("prop_address") and not before:
            enriched += 1

    print(f"  Address enrichment: {enriched}/{len(records)} records populated")
    return records


async def search_doc_type(page, code, date_from, date_to):
    try:
        await page.goto(CLERK_SEARCH, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        await page.wait_for_selector("input, select", timeout=10000)

        # Fill form fields
        text_inputs = await page.query_selector_all("input[type='text'], input:not([type])")
        for inp in text_inputs:
            attrs = " ".join([
                (await inp.get_attribute("placeholder") or "").lower(),
                (await inp.get_attribute("name") or "").lower(),
                (await inp.get_attribute("id") or "").lower(),
            ])
            if any(k in attrs for k in ["doctype", "doc_type", "instrument", "type"]):
                await inp.triple_click()
                await inp.type(code)
            elif any(k in attrs for k in ["datefrom", "date_from", "startdate", "dtfrom", "fromdate"]):
                await inp.triple_click()
                await inp.type(date_from)
            elif any(k in attrs for k in ["dateto", "date_to", "enddate", "dtto", "todate"]):
                await inp.triple_click()
                await inp.type(date_to)

        # Submit
        submit = page.locator("input[type='submit'], button[type='submit'], button:has-text('Search')")
        if await submit.count():
            await submit.first.click()
        else:
            await page.keyboard.press("Enter")

        await page.wait_for_load_state("networkidle", timeout=20000)
        await page.wait_for_timeout(1500)

        return await parse_results(page, code)

    except Exception as e:
        print(f"    ✗ Error on {code}: {e}")
        return []


async def parse_results(page, doc_code):
    records = []
    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        headers = [
            th.get_text(strip=True).lower().replace(" ", "_").replace("#", "num").replace(".", "")
            for th in rows[0].find_all(["th", "td"])
        ]
        hdr_str = " ".join(headers)
        if not any(k in hdr_str for k in ["instrument", "grantor", "grantee", "doc", "instr", "book"]):
            continue

        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cells) < 2 or not any(cells):
                continue

            cm = {headers[i]: cells[i] for i in range(min(len(headers), len(cells)))}

            def get(*keys):
                for k in keys:
                    for h, v in cm.items():
                        if k in h and v and v != "-":
                            return v
                return ""

            doc_num  = get("instrument", "instr", "doc_num", "book_page")
            doc_type = get("type", "doc_type", "doc") or doc_code
            filed    = get("date", "recorded", "record_date", "instr_date")
            grantor  = get("grantor", "party1", "defendant", "owner", "name")
            grantee  = get("grantee", "party2", "plaintiff", "lender")
            legal    = get("legal", "description", "legal_desc", "property")
            consider = get("consideration", "amount", "value", "price")

            amount = None
            m = re.search(r"\$?([\d,]+\.?\d*)", consider)
            if m:
                try:
                    amount = float(m.group(1).replace(",", ""))
                    if amount == 0:
                        amount = None
                except:
                    pass

            filed_iso = ""
            for fmt in ["%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%m/%d/%y"]:
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

            # Extract address clues from legal
            prop_address, prop_city, prop_zip = "", "", ""
            if legal:
                am = re.search(r"(\d{2,5}\s+[A-Z][A-Z0-9\s]{2,25}?(?:ST|AVE|BLVD|DR|RD|LN|WAY|CT|CIR|PL|TER|TRL|PKWY)\b)", legal.upper())
                if am:
                    prop_address = am.group(1).title()
                cm2 = re.search(r"\b(TAMPA|BRANDON|RIVERVIEW|PLANT CITY|VALRICO|SEFFNER|LUTZ|WESLEY CHAPEL|RUSKIN|SUN CITY CENTER|GIBSONTON|WIMAUMA|TEMPLE TERRACE)\b", legal.upper())
                if cm2:
                    prop_city = cm2.group(1).title()
                zm = re.search(r"\b(336\d{2})\b", legal)
                if zm:
                    prop_zip = zm.group(1)

            num_clean = re.sub(r"[^0-9]", "", doc_num)
            clerk_url = f"{CLERK_SEARCH}?instrument={num_clean}" if num_clean else CLERK_SEARCH

            records.append({
                "doc_num":      doc_num,
                "doc_type":     doc_type,
                "filed":        filed_iso,
                "cat":          cat,
                "cat_label":    cat_label,
                "owner":        grantor,
                "grantee":      grantee,
                "amount":       amount,
                "legal":        legal,
                "prop_address": prop_address,
                "prop_city":    prop_city,
                "prop_state":   "FL",
                "prop_zip":     prop_zip,
                "mail_address": "",
                "mail_city":    "",
                "mail_state":   "FL",
                "mail_zip":     "",
                "clerk_url":    clerk_url,
                "flags":        [],
                "score":        0,
            })

    return records


# ── Tax Deed Sales ────────────────────────────────────────────────────────────

def scrape_tax_deeds(lookup):
    print("  Scraping tax deed sales…")
    records = []
    try:
        resp = requests.get(TAX_DEED_URL, timeout=20, headers=HEADERS)
        soup = BeautifulSoup(resp.text, "html.parser")

        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue
            hdrs = [th.get_text(strip=True).lower().replace(" ","_") for th in rows[0].find_all(["th","td"])]
            if not any(k in " ".join(hdrs) for k in ["case","parcel","address","sale"]):
                continue
            for row in rows[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if len(cells) < 2:
                    continue
                rc = {hdrs[i]: cells[i] for i in range(min(len(hdrs), len(cells)))}
                owner = rc.get("owner","") or rc.get("owner_name","") or ""
                addr  = rc.get("property_address","") or rc.get("address","") or rc.get("situs","") or ""
                case  = rc.get("case_number","") or rc.get("case_#","") or ""
                sale_date = rc.get("sale_date","") or rc.get("date","") or ""

                r = {
                    "doc_num": case, "doc_type": "TAX DEED SALE",
                    "filed": sale_date, "cat": "FC", "cat_label": "Tax Deed Foreclosure",
                    "owner": owner, "grantee": "", "amount": None, "legal": "",
                    "prop_address": addr, "prop_city": "Tampa", "prop_state": "FL", "prop_zip": "",
                    "mail_address": "", "mail_city": "", "mail_state": "FL", "mail_zip": "",
                    "clerk_url": TAX_DEED_URL, "flags": [], "score": 0,
                }
                records.append(enrich_from_lookup(r, lookup))

        print(f"    ✓ {len(records)} tax deed records")
    except Exception as e:
        print(f"    ✗ Tax deed error: {e}")
    return records


# ── Score + Flags ─────────────────────────────────────────────────────────────

def compute_flags(r):
    flags = []
    cat   = r.get("cat", "")
    owner = r.get("owner", "").upper()
    amt   = r.get("amount", 0) or 0

    flag_map = {"LP":"Lis pendens","FC":"Pre-foreclosure","JG":"Judgment lien",
                "TL":"Tax lien","ML":"Mechanic lien","TD":"Delinquent taxes","PR":"Probate / estate"}
    if cat in flag_map:
        flags.append(flag_map[cat])
    if re.search(r"\bLLC\b|CORP\b|INC\b|LTD\b|TRUST\b|COMPANY\b", owner):
        flags.append("LLC / corp owner")
    if amt > 50000:
        flags.append("Multiple liens")
    filed = r.get("filed", "")
    if filed:
        try:
            if (datetime.now() - datetime.fromisoformat(filed)).days <= 7:
                flags.append("New this week")
        except:
            pass
    return flags


def score_record(r):
    flags = r.get("flags", [])
    score = 30 + len(flags) * 10
    amt = r.get("amount", 0) or 0
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
    print("  Hillsborough County Property Intel Scraper v2    ")
    print("═══════════════════════════════════════════════════")

    now    = datetime.now(timezone.utc)
    d_to   = now.strftime("%m/%d/%Y")
    d_from = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%m/%d/%Y")
    print(f"  Date range: {d_from} → {d_to}\n")

    print("[1/3] HCPA parcel lookup…")
    lookup = build_parcel_lookup()
    print(f"  ✓ {len(lookup['by_owner']):,} owners | {len(lookup['by_folio']):,} folios\n")

    print("[2/3] Clerk Official Records…")
    clerk_recs = await scrape_clerk(lookup, d_from, d_to)
    print(f"  ✓ {len(clerk_recs)} records\n")

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

    deduped.sort(key=lambda r: (bool(r.get("prop_address")), r.get("score", 0)), reverse=True)

    addr_count = sum(1 for r in deduped if r.get("prop_address"))
    cat_counts = {}
    for r in deduped:
        cat_counts[r["cat"]] = cat_counts.get(r["cat"], 0) + 1

    output = {
        "fetched_at":   now.isoformat(),
        "source":       "Hillsborough County Clerk + HCPA",
        "date_range":   {"from": d_from, "to": d_to},
        "total":        len(deduped),
        "with_address": addr_count,
        "records":      deduped,
    }

    for path in [DASHBOARD / "records.json", DATA / "records.json"]:
        with open(path, "w") as f:
            json.dump(output, f, indent=2, default=str)

    print(f"✅  Saved {len(deduped)} records | {addr_count} with address")
    for cat, cnt in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"   {cat}: {cnt}")


if __name__ == "__main__":
    asyncio.run(main())
