# New test tenders: expected results (keep this file for yourself, don't upload it)

All four tenders are fictional and are NOT in the TenderPilot sample data. They are checked against the
sample company (Falak AI Solutions). Upload each one with the sample company profile selected.

| # | File(s) to upload | Language | Expected decision | Why |
|---|---|---|---|---|
| U1 | `U1_speech_analytics_rfp.pdf` | English | 🟢 BID | AI speech/NLP work the company does; all certificates valid on the 2026-11-26 deadline; P1/P2 meet the "one Arabic NLP project above SAR 1.5M" requirement. If it says CONDITIONAL, check why: it may ask for proof of speech-recognition experience, which is a fair open item. |
| U2 | `U2_transactions_ai_ar.docx` (or the `.pdf`) | Arabic | 🔴 NO BID | Looks like a perfect fit, but **Section 16 (المحتوى المحلي)**, buried in the general provisions, requires local content of at least **60%**. The company's certified score is 52%, and bids below the threshold are excluded. Deadline 1448/06/01هـ = **2026-11-11**. |
| U3 | `U3_contract_review_main_rfp.pdf` **and** `U3_contract_review_annex_b.pdf` together, as one tender | English | 🟡 CONDITIONAL BID | Good fit (contract NLP; P5 was an energy-sector contract classification project). But the deadline is **2027-01-10** and Annex B requires certificates valid on the submission date: the **Zakat certificate expires 2026-12-05** and **ISO 9001 expires 2026-12-31**. Both must be renewed. The requirements are only in the second file. |
| U4 | `U4_cctv_schools_ar.docx` (or the `.pdf`) | Arabic | 🔴 NO BID | Supplying and installing 1,200 CCTV cameras is hardware installation, out of scope. It needs an electrical contractor classification and a security-systems license the company doesn't have. Deadline 1448/07/20هـ = **2026-12-29**. |

## What to point out in the demo
- **U2:** "This tender is in Arabic and looks perfect for us, but the agent found a 60% local content rule in the general provisions and said no bid."
- **U3:** "The requirements were in a separate annex file. It read both and caught two certificates that expire before the deadline."
- **U1:** this matches listing L-2026-005, which the listing screen recommended downloading. Now you show the full document.

## Note on the Arabic PDFs
The Arabic PDFs look correct when opened, but Arabic text extraction from PDFs is fragile (the same problem as the real Etimad PDF).
The Word versions (`.docx`) read perfectly, so use those in the demo unless the PDF reading fix is in place.
