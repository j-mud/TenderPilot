# Using TenderPilot with a real company and real tenders

Put a real company's data in this folder, then pick **my_company** as the data set in the app
(or run `python run.py --data data/my_company`).

## 1. Company files (`company/`)
Replace every `TODO` in these templates with real information:
- `profile.md`: what the company does and does not do, size, Saudization, local content
- `certifications.json`: every certificate with its **expiry date** (YYYY-MM-DD), plus what you do *not* hold
- `past_projects.md`: past projects with client type, value, scope, results, and whether a reference letter exists
- `team.md`: key roles, counts and certifications

The agent can only claim what these files prove, so the more complete they are, the better the bids.

## 2. Public listings (`listings.csv`), optional
On tenders.etimad.sa you can browse the tender list without logging in. Copy the rows you
want to screen into `listings.csv` (open it in Excel and save as **CSV UTF-8**):

| reference | title | entity | activity | deadline | value_sar | url |
|---|---|---|---|---|---|---|

The agent scores each listing and tells you which ones are worth downloading.

## 3. Full tender documents (`tenders/`)
Downloading the full documents (كراسة الشروط والمواصفات) needs a registered supplier account on Etimad,
and some tenders charge a fee. Once downloaded, either:
- drop a single file: `tenders/<tender_id>.pdf`, or
- make a folder per tender with all its files: `tenders/<tender_id>/booklet.pdf, appendix.pdf, boq.xlsx`

Supported: PDF (scanned pages need Tesseract with Arabic installed), DOCX, XLSX, MD, TXT.

Optionally add details from the Etimad page, which override what the agent reads:
`tenders/<tender_id>/meta.json` (or `tenders/<tender_id>.meta.json` next to a single file)
```json
{"title": "…", "issuer": "…", "deadline": "1448-06-10 هـ", "estimated_value_sar": 3800000}
```
Hijri dates are converted automatically.

## Keep purchased documents private
`.gitignore` excludes `data/my_company/tenders/` so you don't publish purchased tender documents
in a public repo. The sample data in `data/sample/` is fictional and safe to publish.
