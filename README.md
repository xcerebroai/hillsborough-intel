# 🏠 Hillsborough Intel — Property Motivation Dashboard

Daily-updated property distress intelligence for Hillsborough County, FL.  
Modeled after the Bexar County Intel dashboard — same layout, same GHL export format.

---

## What it tracks

| Lead Type | Source | Doc Types |
|-----------|--------|-----------|
| **Lis Pendens** | Hillsborough Clerk Official Records | LIS PEN |
| **Foreclosures** | Hillsborough Clerk + Tax Deed Sales | NOFC, CERT SALE, TAX DEED SALE |
| **Judgments** | Hillsborough Clerk Official Records | JUDGMENT, FINAL JDG, CERT JUDGMENT |
| **Tax Liens** | Hillsborough Clerk Official Records | TAX LIEN, IRS LIEN, FEDERAL TAX |
| **Mechanic / HOA Liens** | Hillsborough Clerk Official Records | MECH LIEN, CLAIM OF LIEN, HOA LIEN |
| **Tax Delinquent** | Upload — hillstaxfl.gov delinquent file | TAX DELINQUENT |
| **Probate / Estate** | Hillsborough Clerk Official Records | PROBATE, NOTICE ADMIN |

---

## Lead fields (GHL export format)

Every exported CSV row contains:

| Column | Description |
|--------|-------------|
| First Name | Owner first name |
| Last Name | Owner last name |
| **Mailing Address** | Owner mailing street |
| **Mailing City** | Owner mailing city |
| **Mailing State** | Owner mailing state |
| **Mailing Zip** | Owner mailing zip |
| **Property Address** | Subject property street |
| **Property City** | Subject property city |
| **Property State** | FL |
| **Property Zip** | Subject property zip |
| **Lead Type** | Category (Lis Pendens, Foreclosure, etc.) |
| **Document Type** | Raw doc type from Clerk |
| **Date Filed** | Recording date |
| **Document Number** | Clerk instrument number |
| **Amount / Debt Owed** | Dollar amount on record |
| Years Delinquent | (Tax delinquent only) |
| **Seller Score** | 0–100 motivation score |
| **Motivated Seller Flags** | Pipe-separated flags |
| Source | Hillsborough Clerk |
| Public Records URL | Direct link to document |

---

## Setup

### 1. Push to GitHub
```bash
git init && git add . && git commit -m "init"
git remote add origin https://github.com/YOUR_USER/hillsborough-intel.git
git push -u origin main
```

### 2. Enable GitHub Pages
Settings → Pages → Source → **GitHub Actions** → Save

### 3. Enable workflow write permissions
Settings → Actions → General → Workflow permissions → **Read and write** → Save

### 4. First run
Actions → "Hillsborough Intel — Daily Scrape & Deploy" → **Run workflow**

Your dashboard will be live at:  
`https://YOUR_USER.github.io/hillsborough-intel/`

---

## Local dev / testing

```bash
cd scraper
pip install -r requirements.txt
playwright install chromium --with-deps
python fetch.py
# Then open dashboard/index.html in browser
```

---

## Delinquent tax upload

The Hillsborough County Tax Collector publishes delinquent tax records.  
To get the list:
1. Visit https://www.hillstaxfl.gov/taxes/real-estate-tax/delinquent-property-tax/
2. Request or download the delinquent file (CSV or Excel)
3. Upload via the sidebar in the dashboard

The dashboard auto-detects common column names from the export.  
Supported fields: `situs_address`, `owner_name`, `amount_due`, `tax_year`, `account_number`, `property_class`, `mailing_address`, etc.

---

## Data sources

| Source | URL |
|--------|-----|
| Clerk Official Records | https://publicaccess.hillsclerk.com/oripublicaccess/ |
| Tax Deed Sales | https://www.hillsclerk.com/taxdeeds |
| Tax Roll Search | https://publicaccess.hillsclerk.com/TR/ |
| Delinquent Property Tax | https://www.hillstaxfl.gov/taxes/real-estate-tax/delinquent-property-tax/ |
| Property Appraiser | https://gis.hcpafl.org/propertysearch/ |
| County Tax Portal | https://hillsborough.county-taxes.com/public |
| HCPA Bulk Downloads | https://downloads.hcpafl.org/ |

---

## Repo structure

```
.github/workflows/scrape.yml   # Daily cron: 7 AM UTC
scraper/
  fetch.py                     # Playwright + requests scraper
  requirements.txt
dashboard/
  index.html                   # Full intelligence dashboard
  records.json                 # Generated daily (committed by Action)
data/
  records.json                 # Backup copy
README.md
```
