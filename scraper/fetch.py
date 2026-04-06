"""
Hillsborough County Property Intelligence Scraper
==================================================
Sources:
  1. Hillsborough Clerk Official Records (publicaccess.hillsclerk.com)
     - Lis Pendens (LP)
     - Judgments (JG)
     - Tax Liens / IRS Liens (TL)
     - Mechanic / HOA Liens (ML)
     - Foreclosures (FC)
  2. Tax Deed Sales list (hillsclerk.com/taxdeeds)
  3. HCPA parcel data for mailing address enrichment

Output:  dashboard/records.json  (loaded by index.html)
"""

import json
import re
import time
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ── Try imports; guide user if missing ────────────────────────────────────────
try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError:
    raise SystemExit("❌  Run:  pip install playwright && playwright install chromium --with-deps")

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    raise SystemExit("❌  Run:  pip install requests beautifulsoup4")

# ── Config ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
DASHBOARD = ROOT / "dashboard"
DATA = ROOT / "data"
DASHBOARD.mkdir(exist_ok=True)
DATA.mkdir(exist_ok=True)

CLERK_SEARCH = "https://publicaccess.hillsclerk.com/oripublicaccess/"
TAX_DEED_URL = "https://www.hillsclerk.com/taxdeeds"
HCPA_SEARCH  = "https://gis.hcpafl.org/propertysearch/"

# How many days back to fetch
LOOKBACK_DAYS = 2   # Run daily, grab last 2 days to avoid gaps

# Document types → category mapping
DOC_TYPE_MAP = {
    # Lis Pendens
    "LIS PEN":   ("LP", "Lis Pendens"),
    "LIS PENDENS":("LP", "Lis Pendens"),
    "LP":        ("LP", "Lis Pendens"),
    # Foreclosure
    "FORECLOSURE":("FC","Foreclosure"),
    "NOFC":      ("FC", "Foreclosure"),
    "NOF":       ("FC", "Foreclosure"),
    "CERT TITLE":("FC", "Foreclosure"),
    "CERT SALE": ("FC", "Foreclosure"),
    # Judgments
    "JUDGMENT":  ("JG", "Judgment"),
    "JDG":       ("JG", "Judgment"),
    "FINAL JDG": ("JG", "Judgment"),
    "CERT JUDGMENT":("JG","Judgment"),
    # Tax / IRS Liens
    "TAX LIEN":  ("TL", "Tax Lien"),
    "IRS LIEN":  ("TL", "IRS/Federal Tax Lien"),
    "FED TAX":   ("TL", "Federal Tax Lien"),
    "FEDERAL TAX":("TL","Federal Tax Lien"),
    "STATE TAX": ("TL", "State Tax Lien"),
    "TAX CERT":  ("TL", "Tax Certificate"),
    # Mechanic / HOA Liens
    "MECH LIEN": ("ML", "Mechanic's Lien"),
    "MECHANIC":  ("ML", "Mechanic's Lien"),
    "CLAIM OF LIEN":("ML","Mechanic's Lien"),
    "HOA LIEN":  ("ML", "HOA Lien"),
    "CONDO LIEN":("ML", "Condo Lien"),
    "LIEN":      ("ML", "Lien"),
    # Probate
    "PROBATE":   ("PR", "Probate"),
    "NOTICE ADMIN":("PR","Probate / Administration"),
    # Deeds (background)
    "WARRANTY DEED":("DD","Warranty Deed"),
    "QUIT CLAIM": ("DD", "Quit Claim Deed"),
}

# Motivated seller flag rules
def compute_flags(r: dict) -> list[str]:
    flags = []
    cat = r.get("cat","")
    doc = r.get("doc_type","").upper()
    owner = r.get("owner","").upper()
    amt = r.get("amount", 0) or 0

    if cat == "LP": flags.append("Lis pendens")
    if cat == "FC": flags.append("Pre-foreclosure")
    if cat == "JG": flags.append("Judgment lien")
    if cat == "TL": flags.append("Tax lien")
    if cat == "ML": flags.append("Mechanic lien")
    if cat == "TD": flags.append("Delinquent taxes")
    if cat == "PR": flags.append("Probate / estate")
    if re.search(r"\bLLC\b|CORP|INC\b|LTD\b|TRUST\b|COMPANY\b", owner):
        flags.append("LLC / corp owner")
    if amt and amt > 50000:
        flags.append("Multiple liens")
    now = datetime.now(timezone.utc)
    filed = r.get("filed","")
    if filed:
        try:
            fd = datetime.fromisoformat(filed.replace("Z",""))
            if (now - fd.replace(tzinfo=timezone.utc)).days <= 7:
                flags.append("New this week")
        except: pass
    return flags

def score_record(r: dict) -> int:
    flags = r.get("flags", [])
    score = 30
    score += len(flags) * 10
    amt = r.get("amount", 0) or 0
    if amt > 100000: score += 20
    elif amt > 50000: score += 15
    elif amt > 10000: score += 10
    elif amt > 1000:  score += 5
    # LP + FC = highest distress
    if "Lis pendens" in flags and "Pre-foreclosure" in flags: score += 20
    if "New this week" in flags: score += 5
    return min(100, score)


# ── Clerk official records search via Playwright ──────────────────────────────
async def scrape_clerk_records(pw_page, doc_codes: list[str], date_from: str, date_to: str) -> list[dict]:
    """
    Search the Hillsborough Clerk ORI public access portal for a given
    list of document type codes within the date range.
    Returns a list of raw record dicts.
    """
    records = []
    for code in doc_codes:
        print(f"  → Searching doc type: {code} ({date_from}…{date_to})")
        try:
            await pw_page.goto(CLERK_SEARCH, wait_until="networkidle", timeout=30000)
            await pw_page.wait_for_timeout(1000)

            # Fill in document type field
            doc_field = pw_page.locator("input[name*='docType'], input[id*='docType'], input[placeholder*='type']").first
            if await doc_field.count():
                await doc_field.fill(code)

            # Fill date range
            date_from_field = pw_page.locator("input[name*='dateFrom'], input[id*='dateFrom'], input[placeholder*='from']").first
            date_to_field   = pw_page.locator("input[name*='dateTo'],   input[id*='dateTo'],   input[placeholder*='to']").first
            if await date_from_field.count():
                await date_from_field.fill(date_from)
            if await date_to_field.count():
                await date_to_field.fill(date_to)

            # Submit
            submit = pw_page.locator("button[type=submit], input[type=submit], button:has-text('Search')").first
            if await submit.count():
                await submit.click()
                await pw_page.wait_for_load_state("networkidle", timeout=20000)
            else:
                await pw_page.keyboard.press("Enter")
                await pw_page.wait_for_timeout(3000)

            # Harvest result rows
            recs = await extract_result_rows(pw_page, code)
            print(f"    ✓ {len(recs)} records")
            records.extend(recs)
            await pw_page.wait_for_timeout(500)  # polite pause

        except PWTimeout:
            print(f"    ✗ Timeout on {code}")
        except Exception as e:
            print(f"    ✗ Error on {code}: {e}")

    return records


async def extract_result_rows(page, doc_code: str) -> list[dict]:
    """Parse results table from the clerk search page."""
    records = []
    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")

    # The ORI portal renders results in a table
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th","td"])]
        if not any(k in " ".join(headers) for k in ["instrument","grantor","grantee","doc","date","book"]):
            continue
        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cells) < 3:
                continue
            # Map common column positions — ORI portal varies but usually:
            # instrument#, doc type, date, book, page, grantor, grantee, consideration
            rec = {}
            for i, h in enumerate(headers):
                if i < len(cells):
                    rec[h] = cells[i]

            # Normalize
            doc_num  = rec.get("instrument","") or rec.get("instr #","") or ""
            doc_type = rec.get("doc type","") or rec.get("type","") or doc_code
            filed    = rec.get("date","") or rec.get("recorded date","") or rec.get("instr date","") or ""
            grantor  = rec.get("grantor","") or ""   # typically the owner / defendant
            grantee  = rec.get("grantee","") or ""   # lender / plaintiff
            consid   = rec.get("consideration","") or rec.get("amount","") or ""
            legal    = rec.get("legal","") or rec.get("legal description","") or ""

            # Try to parse amount
            amount = None
            m = re.search(r"[\$]?([\d,]+\.?\d*)", consid.replace(",",""))
            if m:
                try: amount = float(m.group(1).replace(",",""))
                except: pass

            # Try to parse date
            filed_iso = ""
            for fmt in ["%m/%d/%Y","%Y-%m-%d","%m-%d-%Y","%B %d, %Y"]:
                try:
                    filed_iso = datetime.strptime(filed.strip(), fmt).date().isoformat()
                    break
                except: pass

            # Determine category
            dt_upper = doc_type.upper()
            cat, cat_label = "DD", "Document"
            for key, val in DOC_TYPE_MAP.items():
                if key in dt_upper:
                    cat, cat_label = val
                    break

            # Build a link to the specific instrument
            clerk_url = CLERK_SEARCH
            if doc_num:
                clerk_url = f"{CLERK_SEARCH}?instrument={doc_num.replace(' ','')}"

            r = {
                "doc_num":   doc_num,
                "doc_type":  doc_type,
                "filed":     filed_iso,
                "cat":       cat,
                "cat_label": cat_label,
                "owner":     grantor,
                "grantee":   grantee,
                "amount":    amount,
                "legal":     legal,
                # Address fields — enriched later via HCPA
                "prop_address": "",
                "prop_city":    "",
                "prop_state":   "FL",
                "prop_zip":     "",
                "mail_address": "",
                "mail_city":    "",
                "mail_state":   "FL",
                "mail_zip":     "",
                "clerk_url":   clerk_url,
                "flags": [],
                "score": 0,
            }
            records.append(r)

    return records


# ── Tax deed page (static HTML, simpler scrape) ────────────────────────────────
def scrape_tax_deeds() -> list[dict]:
    print("  → Scraping tax deed sales list…")
    try:
        resp = requests.get(TAX_DEED_URL, timeout=20, headers={"User-Agent":"Mozilla/5.0"})
        soup = BeautifulSoup(resp.text, "html.parser")
        records = []
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2: continue
            hdrs = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th","td"])]
            if not any(k in " ".join(hdrs) for k in ["case","parcel","address","sale"]): continue
            for row in rows[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if len(cells) < 2: continue
                rec = {h:cells[i] for i,h in enumerate(hdrs) if i<len(cells)}
                addr = rec.get("property address","") or rec.get("address","")
                case = rec.get("case number","") or rec.get("case #","")
                sale_date = rec.get("sale date","") or ""
                records.append({
                    "doc_num":   case,
                    "doc_type":  "TAX DEED SALE",
                    "filed":     sale_date,
                    "cat":       "FC",
                    "cat_label": "Tax Deed Foreclosure",
                    "owner":     rec.get("owner","") or "",
                    "grantee":   "",
                    "amount":    None,
                    "prop_address": addr,
                    "prop_city":  "Tampa",
                    "prop_state": "FL",
                    "prop_zip":   "",
                    "mail_address":"",
                    "mail_city":  "",
                    "mail_state": "FL",
                    "mail_zip":   "",
                    "clerk_url":  TAX_DEED_URL,
                    "flags": [], "score": 0,
                })
        print(f"    ✓ {len(records)} tax deed records")
        return records
    except Exception as e:
        print(f"    ✗ Tax deed error: {e}")
        return []


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    print("═══════════════════════════════════════════════")
    print("  Hillsborough County Property Intel Scraper   ")
    print("═══════════════════════════════════════════════")

    now   = datetime.now(timezone.utc)
    d_to  = now.strftime("%m/%d/%Y")
    d_from= (now - timedelta(days=LOOKBACK_DAYS)).strftime("%m/%d/%Y")
    print(f"  Date range: {d_from} → {d_to}\n")

    all_records = []

    # 1. Playwright scrape of Clerk official records
    doc_codes = [
        "LIS PEN",      # Lis Pendens
        "NOFC",         # Notice of Foreclosure / Commencement
        "JUDGMENT",     # Judgments
        "MECH LIEN",    # Mechanic's Liens
        "CLAIM OF LIEN",# Mechanic's Liens variant
        "IRS LIEN",     # Federal tax liens
        "TAX LIEN",     # State/county tax liens
        "HOA LIEN",     # HOA liens
    ]

    print("[1/2] Official Records scrape (Playwright)…")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            viewport={"width":1280,"height":800}
        )
        clerk_records = await scrape_clerk_records(page, doc_codes, d_from, d_to)
        await browser.close()

    all_records.extend(clerk_records)

    # 2. Tax deed sales (static HTML)
    print("\n[2/2] Tax deed sales…")
    td_records = scrape_tax_deeds()
    all_records.extend(td_records)

    # Enrich: compute flags + scores
    print(f"\n  Enriching {len(all_records)} records…")
    for r in all_records:
        r["flags"] = compute_flags(r)
        r["score"]  = score_record(r)

    # Deduplicate by doc_num
    seen = set()
    deduped = []
    for r in all_records:
        key = r.get("doc_num","") or id(r)
        if key and key not in seen:
            seen.add(key)
            deduped.append(r)

    # Sort by score desc
    deduped.sort(key=lambda r: r.get("score",0), reverse=True)

    output = {
        "fetched_at": now.isoformat(),
        "source":     "Hillsborough County Clerk of Court",
        "date_range": {"from": d_from, "to": d_to},
        "total":      len(deduped),
        "records":    deduped,
    }

    out_path = DASHBOARD / "records.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    also = DATA / "records.json"
    with open(also, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n✅  Saved {len(deduped)} records → {out_path}")
    cat_counts = {}
    for r in deduped:
        cat_counts[r["cat"]] = cat_counts.get(r["cat"],0)+1
    for cat, cnt in sorted(cat_counts.items(), key=lambda x:-x[1]):
        print(f"   {cat}: {cnt}")


if __name__ == "__main__":
    asyncio.run(main())
