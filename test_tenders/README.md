# Test tenders for the upload feature

These four tenders are **fictional**. They are not part of the sample data: the sample run, `eval.py` and the
agent's prompts never load this folder. They were made to test **Upload a tender** in the app with documents the
agent has never seen.

## How to upload them

In the app, set **Data set** to **Upload a tender** and **Company profile** to **sample**. Upload the file(s) below,
leave the optional fields empty, and click **Analyze this tender**. Upload one tender at a time.

| # | Upload | Language | What it tests |
|---|---|---|---|
| U1 | `U1_speech_analytics_rfp.pdf` | English | A straightforward good fit |
| U2 | `U2_transactions_ai_ar.docx` (or the `.pdf`) | Arabic | A disqualifying rule buried in the general provisions |
| U3 | `U3_contract_review_main_rfp.pdf` **and** `U3_contract_review_annex_b.pdf`, selected together | English | Requirements that are only in the second file |
| U4 | `U4_cctv_schools_ar.docx` (or the `.pdf`) | Arabic | Work that is out of the company's scope |

For U2 and U4, prefer the `.docx`: the Arabic PDFs open fine, but text extraction from Arabic PDFs is less reliable.

The expected decision for each tender, and why, is in [EXPECTED_RESULTS.md](EXPECTED_RESULTS.md).
