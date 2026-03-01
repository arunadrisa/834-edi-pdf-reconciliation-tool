# 834 EDI ↔ PDF Reconciliation Tool

A Python command-line tool that automatically audits healthcare benefit
enrollment data by comparing a raw **ASC X12 834 EDI file** against a
corresponding **PDF enrollment summary**, detecting and reporting any
field-level discrepancies between the two.

---

## Table of Contents

1. [Background — What is an 834 file?](#1-background--what-is-an-834-file)
2. [Why this tool exists](#2-why-this-tool-exists)
3. [Python Setup](#3-python-setup)
   - [Install Python](#31-install-python)
   - [Create a virtual environment](#32-create-a-virtual-environment)
   - [Install dependencies](#33-install-dependencies)
4. [Quick Start](#4-quick-start)
5. [Usage Reference](#5-usage-reference)
6. [How It Works](#6-how-it-works)
7. [Understanding the Report](#7-understanding-the-report)
8. [Adapting to Your PDF Layout](#8-adapting-to-your-pdf-layout)
9. [Real-World Optimisation Guide](#9-real-world-optimisation-guide)
10. [Extending the Tool](#10-extending-the-tool)
11. [Known Limitations](#11-known-limitations)
12. [Troubleshooting](#12-troubleshooting)
13. [Project Structure](#13-project-structure)

---

## 1. Background — What is an 834 file?

The **ASC X12 834** (also written "X12 834" or simply "834") is an
Electronic Data Interchange (EDI) standard used in the United States
healthcare industry to transmit **benefit enrollment and maintenance**
information between employers, benefits administrators, and insurance
carriers.

It is mandated by HIPAA for covered entities when submitting electronic
enrollment data and governs transactions such as:

- Enrolling a new employee in health/dental/vision coverage
- Adding or removing dependents (spouse, children)
- Cancelling coverage for a terminated employee
- Processing life events (marriage, birth, disability)

### Raw EDI structure at a glance

An 834 file is a flat text file with a rigid, delimiter-separated structure:

```
ISA*00*          *00*          *ZZ*EMPLOYER *ZZ*HEALTHPLAN *230301*1200*^*00501*000000001*0*P*:~
GS*BE*EMPLOYER*HEALTHPLAN*20230301*1200*1*X*005010X220A1~
ST*834*0001*005010X220A1~
BGN*00*REF12345*20230301*1200*ET**2~
...
NM1*IL*1*SMITH*JOHN*A**JR*34*123456789~
DMG*D8*19800515*M~
HD*021**HLT*PLAN001*ECV~
...
SE*34*0001~
GE*1*1~
IEA*1*000000001~
```

- **Segments** are separated by `~` (segment terminator)
- **Elements** within a segment are separated by `*` (element separator)
- **Segment tags** are 2–3 letter codes (ISA, GS, NM1, DMG, HD, etc.)
- Both delimiters are declared inside the ISA envelope (not hardcoded)

---

## 2. Why this tool exists

HR systems and carrier portals often generate a **PDF enrollment
confirmation** from the same data that was transmitted in the 834.
These are produced independently — sometimes by a different system or
a third-party administrator — which means they can silently diverge.

Common real-world discrepancies include:

| Source of error | Example |
|---|---|
| PDF generator bug | Date formatted incorrectly (YYYYMMDD not converted) |
| Manual re-entry | Name typed differently in the PDF template |
| Code mapping gap | Coverage level "ECV" shown as blank in PDF |
| Truncation | Long names truncated in PDF but not in EDI |
| Encoding issue | Special characters in names corrupt during PDF generation |
| Stale template | PDF uses outdated group ID after a plan change |

This tool detects all of the above automatically, replacing a tedious
manual comparison process with a reliable automated audit.

---

## 3. Python Setup

### 3.1 Install Python

This tool requires **Python 3.10 or higher**.

#### macOS

macOS ships with an outdated Python 2.x. Install a modern Python via
[Homebrew](https://brew.sh):

```bash
# Install Homebrew if you don't have it
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install Python
brew install python@3.12

# Verify
python3 --version   # should show Python 3.12.x
```

#### Windows

Download and run the official installer from
[python.org/downloads](https://www.python.org/downloads/).

> **Important:** During installation, tick **"Add Python to PATH"** before
> clicking Install Now.

Verify in a new terminal (Command Prompt or PowerShell):

```cmd
python --version   # should show Python 3.10+ 
```

#### Linux (Ubuntu / Debian)

```bash
sudo apt update
sudo apt install python3.12 python3.12-venv python3-pip -y
python3 --version
```

#### All platforms — check your version

```bash
python3 --version
# Must be 3.10 or higher. If you see 2.x, use `python3` instead of `python`.
```

---

### 3.2 Create a virtual environment

A virtual environment isolates this project's dependencies from your
system Python and from other projects.  **Always use one.**

```bash
# Clone or navigate to the project directory
cd edi834-diff

# Create a virtual environment named '.venv'
python3 -m venv .venv

# Activate it
# macOS / Linux:
source .venv/bin/activate

# Windows (Command Prompt):
.venv\Scripts\activate.bat

# Windows (PowerShell):
.venv\Scripts\Activate.ps1
```

You'll see `(.venv)` prepended to your shell prompt when the environment
is active.  All `pip install` and `python` commands below assume the
environment is active.

To deactivate when you're done:

```bash
deactivate
```

---

### 3.3 Install dependencies

With the virtual environment active:

```bash
pip install -r requirements.txt
```

The `requirements.txt` included in this repo contains:

```
pdfplumber>=0.10.0
```

That's the only third-party dependency.  `pdfplumber` wraps `pdfminer.six`
and provides a clean API for extracting text with layout awareness from PDFs.

If you don't have a `requirements.txt`, install directly:

```bash
pip install pdfplumber
```

Verify the installation:

```bash
python -c "import pdfplumber; print('pdfplumber OK')"
```

---

## 4. Quick Start

```bash
# Activate your virtual environment first
source .venv/bin/activate    # macOS/Linux
# or
.venv\Scripts\activate.bat   # Windows

# Run the built-in demo (no files needed)
python edi834_diff.py --demo

# Run with injected errors to see what a FAIL report looks like
python edi834_diff.py --demo --inject-errors

# Compare real files
python edi834_diff.py --edi path/to/enrollment.834 --pdf path/to/summary.pdf

# Save the report to a file
python edi834_diff.py --edi enrollment.834 --pdf summary.pdf --output audit_report.txt
```

---

## 5. Usage Reference

```
python edi834_diff.py [OPTIONS]

Options:
  --edi PATH          Path to the 834 EDI file (plain text, .edi or .834)
  --pdf PATH          Path to the corresponding PDF enrollment summary
  --output PATH       Save the report to this file instead of printing to stdout
  --demo              Run against built-in sample data (no real files needed)
  --inject-errors     Used with --demo: injects 3 deliberate errors into the
                      EDI to demonstrate discrepancy detection
  -h, --help          Show help message and exit
```

### Examples

```bash
# Basic comparison
python edi834_diff.py --edi ./data/Q1_enrollment.834 --pdf ./data/Q1_summary.pdf

# Save report with a datestamp in the filename
python edi834_diff.py \
  --edi ./data/Q1_enrollment.834 \
  --pdf ./data/Q1_summary.pdf \
  --output ./reports/audit_$(date +%Y%m%d).txt

# Demo: verify the tool is working (clean run — should report minimal diffs)
python edi834_diff.py --demo

# Demo: verify error detection is working
python edi834_diff.py --demo --inject-errors
# Expected: FAIL with 3+ ERRORs for Last Name, Date of Birth, ZIP Code
```

---

## 6. How It Works

The reconciliation runs in four stages:

```
┌─────────────────────┐     ┌─────────────────────┐
│   834 EDI File      │     │   PDF Enrollment     │
│   (plain text)      │     │   Summary            │
└────────┬────────────┘     └──────────┬───────────┘
         │                             │
         ▼                             ▼
  parse_edi_834()              parse_pdf_enrollment()
  • Detect delimiters          • Extract text with pdfplumber
  • Walk X12 segments          • Search for labelled fields
  • Decode codes (maps)        • Split subscriber/dependent sections
  • Normalise dates, SSNs      • Normalise SSNs
         │                             │
         ▼                             ▼
  EnrollmentRecord             EnrollmentRecord
         │                             │
         └──────────────┬──────────────┘
                        │
                        ▼
               diff_records()
               • Field-by-field fuzzy comparison
               • Severity classification (ERROR/WARNING)
                        │
                        ▼
               format_report()
               • Group by section
               • Summary table
               • Plain text output
```

### EDI Parser

The EDI parser auto-detects the element separator (almost always `*`)
and segment terminator (almost always `~`) from the ISA envelope, then
walks every segment in order.  It maintains a `current_member` pointer
that is updated each time an `INS` segment is encountered, so all
following segments (NM1, DMG, N3, N4, HD, etc.) are automatically
attributed to the correct subscriber or dependent.

### PDF Parser

The PDF parser uses `pdfplumber` to extract text with spatial layout
preservation (column spacing intact).  It then searches the text for
known label strings using regular expressions, handling two common
patterns: labels and values on the same line, and labels followed by
values on the next line.

The text is split at the boundary between the subscriber and dependent
sections to prevent field values from one person being mistakenly
attributed to the other (both sections use labels like "Last Name",
"Date of Birth", etc.).

### Diff Engine

Every field is compared using a **fuzzy normalisation** step that strips
punctuation, spaces, and differences in case before comparing.  This
means `"SMITH"` matches `"Smith"`, `"123-45-6789"` matches `"123456789"`,
and `"ECV"` matches `"ECV — Employee + Children + Spouse"`.

A mismatch where both values are present is an `ERROR`.  A mismatch
where one side is simply missing is downgraded to `WARNING` (usually
indicates a PDF extraction issue rather than a true data error).

---

## 7. Understanding the Report

```
════════════════════════════════════════════════════════════════════════
  834 EDI ↔ PDF RECONCILIATION REPORT
  Generated : 2024-03-01 14:23:00
  EDI File  : enrollment.834
  PDF File  : enrollment_summary.pdf
════════════════════════════════════════════════════════════════════════

  RESULT  : ✗  FAIL — 3 discrepancy/ies found.
  Errors  : 3
  Warnings: 0
  Info    : 0

────────────────────────────────────────────────────────────────────────

  SECTION: Subscriber
  ······················································
  [ERROR] ✗ Subscriber > Last Name
       EDI : Smyth
       PDF : SMITH

  [ERROR] ✗ Subscriber > Date of Birth
       EDI : June 15, 1980
       PDF : May 15, 1980

  [ERROR] ✗ Subscriber > ZIP Code
       EDI : 62702
       PDF : 62701
```

### Severity meanings

| Severity | Icon | Meaning | Action required |
|---|---|---|---|
| `ERROR` | ✗ | Both values found but don't match | Investigate immediately — likely a real data issue |
| `WARNING` | ⚠ | One side is missing / not extracted | Review — may be a PDF layout issue or genuinely absent field |
| `INFO` | ℹ | Reserved for future use | Informational only |

### Summary table

The report ends with a compact field-by-field table for the most critical
fields:

```
  Field                          EDI Value            PDF Value            Match
  -------------------------------------------------------------------------------
  Subscriber > Last Name         Smyth                SMITH                ✗
  Subscriber > First Name        John                 JOHN                 ✓
  Subscriber > SSN               123-45-6789          (none)               ✗
  Subscriber > Date of Birth     June 15, 1980        May 15, 1980         ✗
  Subscriber > Gender            Male                 Male                 ✓
```

`(none)` means the field was not found in that source.

---

## 8. Adapting to Your PDF Layout

The PDF parser works by searching for **label strings** in the extracted
text.  The labels are hardcoded to match our standard PDF template.  If
your organisation's PDFs use different labels, you need to update them.

### Step 1 — Inspect your PDF's extracted text

```python
import pdfplumber

with pdfplumber.open("your_enrollment_summary.pdf") as pdf:
    for page in pdf.pages:
        print(page.extract_text(layout=True))
```

Look at the raw text output and note the exact label strings used (e.g.
your PDF might say `"Member SSN"` instead of `"SSN"`, or `"Effective
From"` instead of `"Coverage Start Date"`).

### Step 2 — Update the label strings

In `parse_pdf_enrollment()` (around line 400 in the script), find the
`find_pdf_value()` calls and update the label strings:

```python
# Before (standard template labels):
m.ssn = find_pdf_value(t, "SSN")

# After (your custom labels — multiple fallbacks are supported):
m.ssn = find_pdf_value(t, "Member SSN", "Social Security Number", "SSN")
```

### Step 3 — Update the section-split regex

If your PDF uses a different header to separate subscriber and dependent
sections, update the `dep_marker` regex:

```python
# Before:
dep_marker = re.search(
    r"Dependent\s*[\(/].*?Loop|Dependent.*?INS\*N", text, re.IGNORECASE
)

# After (example for a PDF that uses "INSURED DEPENDENT" as the header):
dep_marker = re.search(r"INSURED DEPENDENT", text, re.IGNORECASE)
```

### Step 4 — Test with your files

```bash
python edi834_diff.py --edi sample.834 --pdf your_template.pdf
```

Review the WARNING fields — they typically indicate labels that weren't
found.  Adjust until warnings drop to an acceptable level.

---

## 9. Real-World Optimisation Guide

### Batch processing (many files at once)

The script is designed for single file pairs.  For batch processing,
wrap it in a simple loop:

```python
# batch_audit.py
import os
import glob
from edi834_diff import parse_edi_834, parse_pdf_enrollment, diff_records, format_report

edi_files = glob.glob("./data/*.834")

for edi_path in edi_files:
    # Derive the PDF path from the EDI filename (adjust to your naming convention)
    pdf_path = edi_path.replace(".834", "_summary.pdf")
    if not os.path.exists(pdf_path):
        print(f"SKIP {edi_path} — no matching PDF found")
        continue

    with open(edi_path) as f:
        edi_record = parse_edi_834(f.read())
    pdf_record = parse_pdf_enrollment(pdf_path)
    diffs = diff_records(edi_record, pdf_record)

    report_path = edi_path.replace(".834", "_audit.txt")
    report = format_report(edi_record, pdf_record, diffs, edi_path, pdf_path)
    with open(report_path, "w") as f:
        f.write(report)

    status = "PASS" if not diffs else f"FAIL ({len(diffs)} diffs)"
    print(f"{os.path.basename(edi_path):40s}  {status}")
```

### CI/CD integration

The script exits with code `0` regardless of PASS/FAIL.  To make it
fail the CI build when ERRORs are found, add a check in your pipeline:

```python
# At the end of the script, or in a wrapper:
import sys

errors = [d for d in diffs if d.severity == "ERROR"]
sys.exit(1 if errors else 0)
```

Or in a shell script:

```bash
python edi834_diff.py --edi enrollment.834 --pdf summary.pdf --output report.txt
# Check if "Errors  : 0" appears in the report
grep -q "Errors  : 0" report.txt || exit 1
```

### Multi-member files (many subscribers per transmission)

A real 834 batch file can contain hundreds of subscriber loops per
ISA envelope.  The current parser processes only the **first** subscriber.
To handle multi-subscriber files:

1. Add a loop that detects each `INS*Y` segment as a subscriber boundary
2. Collect each subscriber (with its dependents) into a list of
   `EnrollmentRecord` objects
3. Match PDF pages/sections to subscribers by member ID or name

Sketch:

```python
def parse_edi_834_batch(text: str) -> list[EnrollmentRecord]:
    """Parse all subscriber loops from a single EDI file."""
    records = []
    # ... detect all INS*Y positions ...
    # ... for each, call a single-subscriber parser ...
    return records
```

### Structured output (JSON / CSV)

For downstream processing or dashboards, add a JSON formatter:

```python
import json
from dataclasses import asdict

def to_json(edi: EnrollmentRecord, pdf: EnrollmentRecord, diffs: list) -> str:
    return json.dumps({
        "edi": asdict(edi),
        "pdf": asdict(pdf),
        "discrepancies": [
            {"section": d.section, "field": d.field,
             "edi_value": d.edi_value, "pdf_value": d.pdf_value,
             "severity": d.severity}
            for d in diffs
        ]
    }, indent=2, default=str)
```

### Performance — large PDF files

`pdfplumber` reads the entire PDF into memory.  For very large files
(50+ pages, high-resolution scanned PDFs), consider:

- Processing page-by-page with early stopping once all fields are found
- Using `pdfplumber`'s `crop()` method to target specific regions of the page
- Pre-converting PDFs to text with `pdftotext` (poppler-utils) for speed

```bash
# Fast text extraction outside Python (requires poppler-utils)
pdftotext -layout enrollment_summary.pdf extracted.txt
```

Then read `extracted.txt` as a string and pass directly to `find_pdf_value()`.

### Database logging

For compliance auditing, consider logging every run to a database:

```python
import sqlite3, json
from datetime import datetime

conn = sqlite3.connect("audit_log.db")
conn.execute("""
    CREATE TABLE IF NOT EXISTS audit_runs (
        id INTEGER PRIMARY KEY,
        run_at TEXT,
        edi_file TEXT,
        pdf_file TEXT,
        result TEXT,
        error_count INTEGER,
        warning_count INTEGER,
        diffs_json TEXT
    )
""")

conn.execute("INSERT INTO audit_runs VALUES (?,?,?,?,?,?,?,?)", (
    None,
    datetime.now().isoformat(),
    edi_path,
    pdf_path,
    "FAIL" if any(d.severity == "ERROR" for d in diffs) else "PASS",
    sum(1 for d in diffs if d.severity == "ERROR"),
    sum(1 for d in diffs if d.severity == "WARNING"),
    json.dumps([{"section": d.section, "field": d.field,
                 "edi": d.edi_value, "pdf": d.pdf_value} for d in diffs])
))
conn.commit()
```

### Fuzzy name matching

The current normaliser is case- and punctuation-insensitive but does
not handle transpositions or abbreviations.  For environments where
names may have been entered with slight variations (e.g. "O'Brien" vs
"OBrien", or "Katherine" vs "Katharine"), add fuzzy matching:

```bash
pip install rapidfuzz
```

```python
from rapidfuzz import fuzz

def values_match_fuzzy(a, b, threshold=90):
    """Match if normalised similarity score >= threshold (0–100)."""
    na, nb = normalize_for_compare(a), normalize_for_compare(b)
    if na == nb:
        return True
    score = fuzz.ratio(na, nb)
    return score >= threshold
```

Replace `values_match()` calls with `values_match_fuzzy()` as needed.

---

## 10. Extending the Tool

### Adding new fields to check

1. Add the field to the appropriate dataclass (`Member`, `Coverage`, or
   `EnrollmentRecord`)
2. Parse it in `parse_edi_834()` from the correct segment/element
3. Extract it in `parse_pdf_enrollment()` via `find_pdf_value()`
4. Add a `check()` call in `diff_records()` with appropriate severity
5. Optionally add it to the summary table in `format_report()`

### Supporting multiple coverage lines per member (health + dental + vision)

Change `Member.coverage` from `Optional[Coverage]` to `list[Coverage]`
and update `parse_edi_834()` to append each new `Coverage` when an `HD`
segment is encountered.  Update `diff_records()` to compare coverage
lists by insurance line type.

### Adding dental / vision 834 support

The tool already parses `HD*DEN` and `HD*VIS` lines via `INSURANCE_LINE_MAP`.
To reconcile against separate dental/vision PDFs, pass those PDFs as
additional `--pdf` arguments and extend the CLI parser accordingly.

### Email alerts for failures

```python
import smtplib
from email.message import EmailMessage

def send_alert(report: str, to_addr: str):
    msg = EmailMessage()
    msg["Subject"] = "834 Reconciliation FAILED — action required"
    msg["From"] = "alerts@yourcompany.com"
    msg["To"] = to_addr
    msg.set_content(report)
    with smtplib.SMTP("smtp.yourcompany.com") as s:
        s.send_message(msg)
```

---

## 11. Known Limitations

| Limitation | Impact | Workaround |
|---|---|---|
| Single subscriber per run | Can't batch-process multi-subscriber files natively | Use the batch_audit.py wrapper (see §9) |
| Single coverage line per member | Second HD segment (e.g. dental) overwrites health | Extend `Member.coverage` to a list |
| PDF label-based extraction | Fragile to layout changes | Update label strings after any PDF template change |
| No Loop 2700 support | Member reporting categories ignored | Add REF/N1 parsing inside LS/LE blocks if needed |
| No COB support | Coordination of Benefits not checked | Add COB segment parsing for dual-coverage members |
| Windows `%-d` date format | `strftime("%-d")` fails on Windows | Replace with `strftime("%#d")` or `int(d)` |
| Scanned / image-only PDFs | pdfplumber can't extract text | Pre-process with OCR (Tesseract) before running |

### Windows date formatting fix

If you see a `ValueError: Invalid format string` on Windows when parsing
dates, replace the `%-d` format specifier in `normalize_date_edi()`:

```python
# Windows-compatible version:
def normalize_date_edi(d: str) -> str:
    try:
        dt = datetime.strptime(d.strip(), "%Y%m%d")
        return f"{dt.strftime('%B')} {dt.day}, {dt.strftime('%Y')}"
    except Exception:
        return d.strip()
```

---

## 12. Troubleshooting

### "pdfplumber is required" error

You haven't installed the dependency, or you're running Python from
outside the virtual environment:

```bash
# Make sure your venv is active
source .venv/bin/activate

# Then install
pip install pdfplumber

# Confirm
pip list | grep pdfplumber
```

### All PDF fields show as `(not set)` / WARNING

The PDF parser couldn't find the expected labels.  Run the text
inspection snippet from §8 Step 1 to see what the raw extracted text
looks like, then update the label strings accordingly.

### EDI fields show as `(not set)` / WARNING

The EDI delimiter auto-detection may have failed.  Check that:
- Your file starts with `ISA` (no BOM or leading whitespace)
- The file uses standard `*` element separators and `~` segment terminators
- The file is not binary / base64-encoded

Quick sanity check:
```bash
head -c 200 enrollment.834 | cat -v
```
You should see readable text with `*` and `~` characters.

### Report shows many WARNING for fields that clearly exist in the PDF

The label regex is not matching.  The most common causes:
- Extra spaces in the PDF label (e.g. `"Last  Name"` with two spaces)
- Unicode characters in the PDF (e.g. non-breaking spaces, en-dashes)
- The label is split across two lines in the PDF

Try printing the raw extracted text and searching for your label manually
to see exactly how it appears.

### `ValueError: Invalid format string` on Windows

See the "Known Limitations" section above for the Windows date fix.

---

## 13. Project Structure

```
edi834-diff/
├── edi834_diff.py          # Main script — parser, diff engine, report formatter
├── generate_834_pdf.py     # Companion script — generates sample PDF for testing
├── requirements.txt        # Python dependencies (pdfplumber)
├── README.md               # This file
└── sample_data/            # (optional) Place your test .834 and .pdf files here
    ├── enrollment.834
    └── enrollment_summary.pdf
```

---

## Contributing

When adding new features or fixing bugs, please:

1. Keep all new functions documented with docstrings in the same style
   as the existing code
2. Add or update the relevant section in this README
3. Test with both `--demo` (clean) and `--demo --inject-errors` to confirm
   existing detection still works
4. If you add a new field to check, document its X12 segment and element
   reference in the docstring

---

## Licence

Internal use only. Not for redistribution.
