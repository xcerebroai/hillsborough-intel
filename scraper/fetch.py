"""
Hillsborough County Property Intel Scraper v7
====================================================
Fixes:
  1. Name matching: try both "FIRST LAST" and "LAST FIRST" formats
  2. Add all relevant doc types from the confirmed 64-option list:
     (LN) LIEN, (MEDLN) MEDICAID LIEN, (NOC) NOTICE OF COMMENCEMENT,
     (FIN) FINANCING STATEMENT, (MTG) MORTGAGE
  3. Also match by folio extracted from legal description
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
    from playwright.async_api import async_playwright
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
})

DOC_TYPE_MAP = {
    "LIS PEN":       ("LP", "Lis Pendens"),
    "LIS PENDENS":   ("LP", "Lis Pendens"),
    "FORECLOS":      ("FC", "Foreclosure"),
    "TAX DEED":      ("FC", "Tax Deed"),
    "JUDGMENT":      ("JG", "Judgment"),
    "TAX LIEN":      ("TL", "Tax Lien"),
    "CORP TAX":      ("TL", "Corp Tax Lien"),
    "MEDICAID":      ("TL", "Medicaid Lien"),
    "LIEN":          ("ML", "Lien"),
    "PROBATE":       ("PR", "Probate"),
}

# Exact values from the confirmed 64-option dropdown
TARGET_KEYWORDS = {
    # Lis Pendens
    "LP":        ("LP", "Lis Pendens"),
    "LIS PEN":   ("LP", "Lis Pendens"),
    "RELLP":     ("LP", "Release Lis Pendens"),
    # Foreclosure / Tax Deed
    "TAXDEED":   ("FC", "Tax Deed"),
    "TAX DEED":  ("FC", "Tax Deed"),
    "NOFC":      ("FC", "Foreclosure"),
    # Judgments
    "JUD":       ("JG", "Judgment"),
    "CCJ":       ("JG", "Certified Court Judgment"),
    "DRJUD":     ("JG", "Domestic Relations Judgment"),
    # Tax / Government Liens
    "LNCORPTX":  ("TL", "Corp Tax Lien"),
    "MEDLN":     ("TL", "Medicaid Lien"),
    "SATCORPTX": ("TL", "Sat Corp Tax Lien"),
    # General Liens
    "LN":        ("ML", "Lien"),
    "LIEN":      ("ML", "Lien"),
    "NCL":       ("ML", "Notice of Contest of Lien"),
    # Probate
    "PRO":       ("PR", "Probate"),
    # Notices / Commencement (pre-construction liens)
    "NOC":       ("ML", "Notice of Commencement"),
    "NOCCONS":   ("ML", "Notice of Commencement Consumer"),
    # Condo / HOA
    "CND":       ("ML", "Condo Declaration"),
    "CONDO":     ("ML", "Condo Plan"),
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
        print(f"    ✗ {e}")
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


def parse_dbf_from_zip(content):
    rows = []
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            dbf_files = [n for n in z.namelist() if n.lower().endswith(".dbf")]
            if not dbf_files or not HAS_DBF:
                return []
            # Prefer parcel_4_public.dbf over others
            fname = next((f for f in dbf_files if "parcel_4" in f.lower()), dbf_files[0])
            with z.open(fname) as f:
                dbf_bytes = f.read()
            import tempfile, os
            with tempfile.NamedTemporaryFile(suffix=".dbf", delete=False) as tmp:
                tmp.write(dbf_bytes)
                tmp_path = tmp.name
            try:
                table = DBF(tmp_path, encoding="latin-1", ignore_missing_memofile=True)
                rows = [dict(r) for r in table]
            finally:
                os.unlink(tmp_path)
    except Exception as e:
        print(f"    ✗ DBF error: {e}")
    return rows


def normalize_name(name):
    """Normalize name for lookup — strip punctuation, uppercase."""
    name = str(name).upper().strip()
    for sfx in [" JR"," SR"," II"," III"," IV"," TRUST"," TR"," ET AL"," ET UX"," ETAL"," LLC"," INC"," CORP"," LTD"]:
        name = name.replace(sfx,"")
    name = re.sub(r"[^A-Z ]","",name)
    return re.sub(r"\s+"," ",name).strip()


def name_variants(name):
    """Generate lookup variants: original, reversed (last first → first last)."""
    n = normalize_name(name)
    if not n:
        return []
    variants = [n]
    parts = n.split()
    if len(parts) >= 2:
        # Try "LAST FIRST MIDDLE" → "FIRST MIDDLE LAST"
        variants.append(" ".join(parts[1:] + [parts[0]]))
        # Try "FIRST LAST"
        variants.append(parts[0] + " " + parts[-1])
        # Try "LAST FIRST"
        variants.append(parts[-1] + " " + parts[0])
    return variants


def build_parcel_lookup():
    print("  Fetching HCPA file list…")
    files, soup = hcpa_get_files()
    if not files:
        return {"by_owner":{},"by_folio":{}}

    by_owner, by_folio = {}, {}
    rows = []

    for name, target in files.items():
        if re.search(r"HCparcel_4_public", name, re.I):
            print(f"  Downloading {name}…")
            content = hcpa_post_download(target, soup)
            if content and content[:2] == b'PK':
                rows = parse_dbf_from_zip(content)
                if rows:
                    print(f"  ✓ {len(rows):,} parcel rows")
                    print(f"  Columns: {list(rows[0].keys())[:10]}")
            break

    if not rows:
        print("  ✗ No parcel data")
        return {"by_owner":{},"by_folio":{}}

    for row in rows:
        folio     = str(row.get("FOLIO","") or "").strip()
        owner     = str(row.get("OWNER","") or "").strip()
        # Mailing address
        mail_addr = str(row.get("ADDR_1","") or "").strip()
        mail_addr2= str(row.get("ADDR_2","") or "").strip()
        if mail_addr2 and mail_addr2 != mail_addr:
            mail_full = mail_addr
        else:
            mail_full = mail_addr
        mail_city  = str(row.get("CITY","") or "Tampa").strip()
        mail_state = str(row.get("STATE","") or "FL").strip()
        mail_zip   = str(row.get("ZIP","") or "").strip()
        # Property / site address
        site_addr  = str(row.get("SITE_ADDR","") or "").strip()
        site_city  = str(row.get("SITE_CITY","") or mail_city or "Tampa").strip()
        site_zip   = str(row.get("SITE_ZIP","") or mail_zip or "").strip()

        info = {
            "prop_address": site_addr or mail_full,
            "prop_city":    site_city or "Tampa",
            "prop_state":   "FL",
            "prop_zip":     site_zip,
            "mail_address": mail_full,
            "mail_city":    mail_city or "Tampa",
            "mail_state":   mail_state or "FL",
            "mail_zip":     mail_zip or site_zip,
        }

        if folio:
            by_folio[folio] = info
        if owner:
            # Index ALL name variants for better matching
            for variant in name_variants(owner):
                if len(variant) > 3 and variant not in by_owner:
                    by_owner[variant] = info

    print(f"  ✓ {len(by_owner):,} name variants | {len(by_folio):,} folios")
    return {"by_owner": by_owner, "by_folio": by_folio}


def enrich(record, lookup):
    if record.get("prop_address"):
        return record

    # Try all name variants
    for variant in name_variants(record.get("owner","")):
        if variant in lookup["by_owner"]:
            info = lookup["by_owner"][variant]
            for k, v in info.items():
                if not record.get(k):
                    record[k] = v
            return record

    # Try folio from legal description
    legal = record.get("legal","")
    if legal:
        fm = re.search(r"\b(\d{10})\b", legal)
        if fm and fm.group(1) in lookup["by_folio"]:
            info = lookup["by_folio"][fm.group(1)]
            for k, v in info.items():
                if not record.get(k):
                    record[k] = v
            return record

        # Extract address from legal text
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

            print(f"  Matched {len(matched)} doc types:")
            for o in matched:
                print(f"    {o['val']!r} → {o['cat']}")

        except Exception as e:
            print(f"  ✗ {e}")
            await browser.close()
            return []

        for opt in matched:
            print(f"\n  → {opt['txt']}")
            try:
                await page.goto("https://publicaccess.hillsclerk.com/oripublicaccess/",
                               wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(1500)

                await page.evaluate(f"""() => {{
                    const d = document;
                    const s = d.querySelector('#OBKey__1285_1,[name="OBKey__1285_1"]');
                    if(s){{ s.value={json.dumps(opt['val'])}; s.dispatchEvent(new Event('change',{{bubbles:true}})); }}
                    const df = d.querySelector('#OBKey__1634_1,[name="OBKey__1634_1"]');
                    if(df){{ df.value={json.dumps(date_from)}; df.dispatchEvent(new Event('change',{{bubbles:true}})); }}
                    const dt = d.querySelector('#OBKey__1634_2,[name="OBKey__1634_2"]');
                    if(dt){{ dt.value={json.dumps(date_to)}; dt.dispatchEvent(new Event('change',{{bubbles:true}})); }}
                    for(const b of d.querySelectorAll('input[type=submit],button[type=submit],button')){{
                        const t=(b.value||b.textContent||'').toLowerCase();
                        if(t.includes('search')||t.includes('submit')){{ b.click(); return; }}
                    }}
                    const f=d.querySelector('form'); if(f) f.submit();
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
    flag_map = {
        "LP":"Lis pendens","FC":"Pre-foreclosure","JG":"Judgment lien",
        "TL":"Tax lien","ML":"Mechanic lien","TD":"Delinquent taxes","PR":"Probate / estate"
    }
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
    print("  Hillsborough County Property Intel Scraper v7    ")
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
