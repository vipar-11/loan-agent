# Apex Financial — Loan Application Batch File Specification

**Version 1.0** · For approved origination partners · Sample file: [`loan_batch_sample.csv`](loan_batch_sample.csv)

Approved partners may submit personal installment loan applications in bulk by delivering CSV batch files to
their Apex Financial FTP folder. Every valid row becomes a loan application in our underwriting queue; every
invalid row is flagged, **not submitted**, and reported back to you with the reason.

---

## 1. Delivery

| Item | Rule |
| :--- | :--- |
| Where | Your FTP home folder (your **inbox**), which is provisioned for your partner ID, e.g. `ACME_BROKERS/`. Files delivered anywhere else are not read. |
| File name | Must end in `.csv`. Recommended: `<PARTNER_ID>_<YYYYMMDD>_<NNN>.csv`, e.g. `ACME_BROKERS_20260115_001.csv`. |
| Upload procedure | Upload under a temporary name that does **not** end in `.csv` (e.g. `ACME_BROKERS_20260115_001.csv.part`), then **rename** it to the final `.csv` name once the transfer is complete. A `.csv` file is only picked up after it has been unchanged for a short settling period. |
| Size limits | At most **1,000 application rows** and **5 MB** per file. Split larger volumes into several files. |
| Pick-up | Files are picked up automatically within about a minute and removed from your inbox once picked up. |
| Resending | Re-delivering a byte-identical file is recognised and **not** processed again (reported as `DUPLICATE_FILE`). To correct flagged rows, send them in a **new** file. |

## 2. File format

* Text encoding **UTF-8** (a byte-order mark is tolerated). Comma (`,`) delimiter. Line endings CRLF or LF.
* Standard CSV quoting (RFC 4180): enclose a value in double quotes if it contains a comma, double quote or line
  break; escape a double quote by doubling it (`"Some ""Nickname"" Name"`).
* **First row: header row** with exactly the seven column names below (any order, case-insensitive). Missing,
  unknown or repeated columns cause the whole file to be rejected.
* One application per row. Blank lines are ignored.

## 3. Columns

All columns are **required** on every row.

| Column | Format | Rules |
| :--- | :--- | :--- |
| `partner_reference` | 1–64 characters: letters, digits, `.`, `_`, `-` (must start with a letter or digit) | **Your** unique ID for the application. Must be unique within the file and is never accepted twice from you — a reference that was already accepted is flagged as a duplicate. Used to match our results to your records. Case-sensitive. |
| `applicant_name` | Text, 1–120 characters | Borrower's full legal name. Quote it if it contains a comma, e.g. `"Some Name, Jr."`. |
| `ssn_last_4` | Exactly 4 digits | Last four digits of the borrower's SSN. **Keep leading zeros** (`0420`); make sure spreadsheet tools do not turn it into a number. |
| `stated_annual_income` | Plain decimal, up to 2 decimals | Gross annual income in USD. Greater than 0 and at most 10,000,000. No `$`, no thousands separators (`85000.00`, not `$85,000`). |
| `loan_amount_requested` | Plain decimal, up to 2 decimals | Requested principal in USD, **1,000.00 – 50,000.00**. Fixed 36-month term at 12.0% APR. |
| `loan_purpose` | One of the listed values (case-insensitive) | `Home Improvement`, `Debt Consolidation`, `Medical Expenses`, `Major Purchase`, `Business`, `Other` |
| `fcra_consent` | `Y` or `N` | Whether the borrower authorised the credit bureau inquiry and bank cash-flow review (FCRA / GLBA). Applications with `N` are accepted but are declined without any credit inquiry (`REJECTED_NO_CONSENT`). |

### Example

```csv
partner_reference,applicant_name,ssn_last_4,stated_annual_income,loan_amount_requested,loan_purpose,fcra_consent
ACME-2026-000101,Mrs. Some Name,1111,96000.00,15000.00,Home Improvement,Y
ACME-2026-000104,"Some Name, Jr.",1111,88500.50,12000.00,Medical Expenses,Y
```

## 4. Processing and results

Each row is validated independently:

* **ACCEPTED** — the row is valid and has been submitted as a new loan application. You receive our application ID.
* **FLAGGED** — the row has missing or invalid data (or a duplicate `partner_reference`). It is **not submitted**.
  Every problem found in the row is listed, so it can be fixed in one pass.

The rest of the file is still processed when individual rows are flagged. Problems with the file as a whole
(wrong encoding, invalid header, too many rows, too large, empty) reject the entire file and no row is submitted.

For every file we pick up, a **results file** is placed in your **outbox** folder:
`<your file name without .csv>.batch-<batch number>.results.csv`

| Column | Meaning |
| :--- | :--- |
| `row_number` | 1-based position of the application row after the header (blank lines not counted). Empty for file-level results. |
| `partner_reference` | Your reference, as sent. |
| `status` | `ACCEPTED`, `FLAGGED`, or — for the whole file — `FILE_REJECTED` / `DUPLICATE_FILE`. |
| `application_id` | Apex Financial application ID for accepted rows. |
| `messages` | Reasons a row or file was not accepted. Each reason starts with the column it concerns (e.g. `ssn_last_4: …`); several reasons are separated by ` \| `. |

Results files never contain borrower personal data. Accepted applications then go through our standard
underwriting; credit decisions are communicated through the usual channels, not in the results file.

## 5. Security & data handling

Batch files contain consumer personal information. Deliver them only over the secured connection provisioned for
you, do not include data beyond the columns above, and delete local copies according to your data-retention
obligations under GLBA.
