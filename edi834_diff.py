"""
edi834_diff.py — 834 EDI ↔ PDF Reconciliation Tool
=====================================================

PURPOSE
-------
This script is an audit / reconciliation tool for healthcare benefit
enrollment data. It ingests two representations of the same enrollment
transaction and checks that they agree:

  1. A raw ASC X12 834 EDI file  — the authoritative source produced by
     an HR / benefits administration system and transmitted to a health
     insurance carrier.

  2. A PDF enrollment summary    — a human-readable document generated
     from the same data, used internally for recordkeeping, employee
     confirmation letters, or carrier acknowledgment packets.

Because the PDF is produced independently (by a report generator, a
carrier portal, or a third-party admin), it can silently diverge from
the EDI. This tool detects those divergences automatically so the team
doesn't have to compare them manually.

HOW IT WORKS (high level)
--------------------------
  parse_edi_834()        → walks every X12 segment, decodes codes,
                           normalises dates/SSNs → EnrollmentRecord
        ↓
  parse_pdf_enrollment() → extracts text from the PDF with pdfplumber,
                           searches for labelled fields → EnrollmentRecord
        ↓
  diff_records()         → field-by-field fuzzy comparison → [Discrepancy]
        ↓
  format_report()        → human-readable text report (stdout or file)

SUPPORTED X12 SEGMENTS
-----------------------
  Envelope  : ISA, GS, ST, SE, GE, IEA
  Header    : BGN, REF(38/1L), DTP(007)
  Loops     : N1(P5/IN/TV), INS, REF(0F/1L/1W), NM1(IL),
              PER, N3, N4, DMG, HD, DTP(348/349/356), LS, LX, LE

USAGE
-----
  # Compare real files
  python edi834_diff.py --edi enrollment.834 --pdf enrollment_summary.pdf

  # Save the report to a file instead of printing
  python edi834_diff.py --edi enrollment.834 --pdf summary.pdf --output report.txt

  # Run against built-in sample data (no files needed)
  python edi834_diff.py --demo

  # Same demo but with 3 deliberate errors injected into the EDI
  python edi834_diff.py --demo --inject-errors

DEPENDENCIES
------------
  pip install pdfplumber          # PDF text extraction
  Python 3.10+ recommended        # uses match-style type hints

EXIT CODES
----------
  0  — completed (check report for PASS/FAIL status)
  1  — fatal error (missing file, import error, bad arguments)
"""

# ── Standard library imports ──────────────────────────────────────────────────
import re           # regex for delimiter detection, field extraction, normalisation
import sys          # sys.exit() for fatal errors
import argparse     # CLI argument parsing
import textwrap     # for multi-line help text in argparse epilog
from dataclasses import dataclass, field   # typed, self-documenting data containers
from typing import Optional                # nullable field type hints
from datetime import datetime              # date parsing and formatting

# ── Third-party import (with helpful error message if missing) ────────────────
try:
    import pdfplumber   # PDF text extraction library; install with: pip install pdfplumber
except ImportError:
    print("ERROR: pdfplumber is required.  Run:  pip install pdfplumber")
    sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — DATA MODELS
# ══════════════════════════════════════════════════════════════════════════════
#
# We use Python dataclasses as plain data containers (no business logic).
# All fields default to None so a partially-populated object is valid —
# the diff engine treats None as "not found / not extracted" and flags it
# as a WARNING rather than an ERROR.
#
# The hierarchy mirrors the X12 834 loop structure:
#   EnrollmentRecord
#     ├── (transmission metadata)        → ISA / GS / ST / BGN
#     ├── (sponsor / carrier identity)   → N1 loops 1000A + 1000B
#     ├── subscriber: Member             → INS*Y loop 2000
#     │     └── coverage: Coverage      → HD / DTP loops 2300
#     └── dependents: [Member]           → INS*N loop 2000 (one per dependent)
#           └── coverage: Coverage
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Coverage:
    """
    Represents a single health coverage line (Loop 2300 / HD segment).

    One Member can theoretically have multiple coverage lines (health +
    dental + vision), but this implementation tracks one per member —
    the first HD segment encountered.  See REAL-WORLD NOTES in README for
    how to extend to multi-coverage.
    """
    plan_id: Optional[str] = None           # HD05 — plan/product ID (e.g. "PLAN001")
    start_date: Optional[str] = None        # DTP*348 — coverage effective date
    end_date: Optional[str] = None          # DTP*349 — coverage termination date
    coverage_level: Optional[str] = None    # HD05 — e.g. "ECV", "EMP", "FAM"
    insurance_line: Optional[str] = None    # HD03 — e.g. "HLT", "DEN", "VIS"
    maintenance_type: Optional[str] = None  # HD01 — e.g. "021" (Change of Status)


@dataclass
class Member:
    """
    Represents one insured person: either the subscriber (employee) or a
    dependent (spouse, child, etc.).

    Maps to the INS/NM1/DMG/N3/N4/PER segments within a Loop 2000 block.
    The 'role' field distinguishes subscriber (INS01='Y') from dependent
    (INS01='N') so the diff engine can apply role-specific logic (e.g.
    address fields are only checked for the subscriber).
    """
    role: str = ""                   # "subscriber" or "dependent" — set from INS01
    last_name: Optional[str] = None  # NM103
    first_name: Optional[str] = None # NM104
    middle_initial: Optional[str] = None  # NM105
    suffix: Optional[str] = None     # NM107 (e.g. "JR", "SR")
    ssn: Optional[str] = None        # NM109 when NM108='34' (Social Security Number)
    member_id: Optional[str] = None  # REF*0F — employer-assigned employee/dependent ID
    dob: Optional[str] = None        # DMG02 — date of birth (YYYYMMDD in EDI)
    gender: Optional[str] = None     # DMG03 — "M" / "F" / "U" in EDI
    relationship_code: Optional[str] = None  # INS02 — "18"=self, "01"=spouse, "19"=child
    benefit_status: Optional[str] = None     # INS05 — "A"=active, "C"=COBRA, etc.
    maintenance_type: Optional[str] = None   # INS03 — type of change, e.g. "021"
    employment_status: Optional[str] = None  # INS08 — "FT"=full-time, "PT"=part-time
    # Address fields (subscriber only — dependents typically share subscriber address)
    address_line1: Optional[str] = None  # N301
    address_line2: Optional[str] = None  # N302 (apt / suite)
    city: Optional[str] = None           # N401
    state: Optional[str] = None          # N402 (2-letter state code)
    zip_code: Optional[str] = None       # N403
    country: Optional[str] = None        # N404 (e.g. "US")
    phone: Optional[str] = None          # PER*IP*TE — contact phone number
    coverage: Optional[Coverage] = None  # nested coverage info from HD + DTP segments


@dataclass
class EnrollmentRecord:
    """
    Top-level container representing one complete 834 transaction
    (one ISA/GS/ST envelope with one subscriber and zero or more dependents).

    In a real 834 file a single ISA envelope can contain multiple
    subscriber groups.  This implementation processes one subscriber loop.
    See README for batch-processing guidance.
    """
    # ── Interchange / Transmission metadata ──────────────────────────────────
    interchange_control_num: Optional[str] = None  # ISA13 — unique per transmission
    transaction_set_num: Optional[str] = None      # ST02 — e.g. "0001"
    transmission_date: Optional[str] = None        # ISA09 — date of ISA envelope
    transmission_time: Optional[str] = None        # ISA10 — time of ISA envelope
    file_reference_num: Optional[str] = None       # BGN02 — unique reference / batch ID
    submission_type: Optional[str] = None          # BGN01 — "00"=new, "01"=cancel, etc.
    effective_date: Optional[str] = None           # BGN03 or DTP*007 — file effective date

    # ── Plan Sponsor (employer) — Loop 1000A ─────────────────────────────────
    sponsor_name: Optional[str] = None  # N1*P5 — N102
    sponsor_ein: Optional[str] = None   # N1*P5 — N104 when N103='FI' (Federal Tax ID)
    group_id: Optional[str] = None      # REF*38 — group / plan ID assigned by carrier

    # ── Insurance Carrier (payer) — Loop 1000B ───────────────────────────────
    carrier_name: Optional[str] = None  # N1*IN — N102
    carrier_id: Optional[str] = None    # N1*IN — N104

    # ── Members ───────────────────────────────────────────────────────────────
    subscriber: Optional[Member] = None         # single subscriber (INS01='Y')
    dependents: list = field(default_factory=list)  # zero or more dependents

    # ── Transaction Trailer ───────────────────────────────────────────────────
    segment_count: Optional[str] = None          # SE01 — total segment count in the ST-SE envelope
    functional_group_count: Optional[str] = None # GE01 — number of transaction sets in the GS-GE group


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — EDI CODE LOOKUP TABLES
# ══════════════════════════════════════════════════════════════════════════════
#
# X12 EDI uses short codes everywhere (e.g. "M" for Male, "021" for
# Change of Status).  These dictionaries translate the raw codes into
# human-readable labels so comparisons against PDF text work reliably.
#
# Source: ASC X12 005010X220A1 Implementation Guide for the 834 transaction.
# Extend these as needed when you encounter new code values in production files.
# ──────────────────────────────────────────────────────────────────────────────

# DMG03 — biological sex code
GENDER_MAP = {
    "M": "Male",
    "F": "Female",
    "U": "Unknown",
}

# INS03 — type of enrollment change being reported
MAINTENANCE_TYPE_MAP = {
    "001": "Change",
    "021": "Change of Status",
    "024": "Cancel or Terminate",
    "025": "Reinstatement",
    "030": "Audit or Compare",
    "032": "Employee Information Not Applicable",
}

# INS02 — insured's relationship to the subscriber
RELATIONSHIP_MAP = {
    "18": "18 — Self",
    "01": "01 — Spouse",
    "19": "19 — Child",
    "15": "15 — Ward",
    "17": "17 — Life Partner",
    "G8": "G8 — Other Relationship",
}

# INS05 — current coverage / benefit status of the insured
BENEFIT_STATUS_MAP = {
    "A": "A — Active",
    "C": "C — COBRA",
    "T": "T — Surviving Insured",
    "U": "U — Unknown",
}

# HD05 — coverage type / tier code
COVERAGE_LEVEL_MAP = {
    "ECV": "ECV — Employee + Children + Spouse",
    "ESP": "ESP — Employee + Spouse",
    "ECH": "ECH — Employee + Children",
    "EMP": "EMP — Employee Only",
    "FAM": "FAM — Family",
    "IND": "IND — Individual",
}

# HD03 — line of insurance (product type)
INSURANCE_LINE_MAP = {
    "HLT": "HLT — Health",
    "DEN": "DEN — Dental",
    "VIS": "VIS — Vision",
    "LIF": "LIF — Life",
    "STD": "STD — Short-Term Disability",
    "LTD": "LTD — Long-Term Disability",
}

# BGN01 — purpose / submission type of the file
BGN_TYPE_MAP = {
    "00": "00 — New Enrollment",
    "01": "01 — Cancellation",
    "02": "02 — Audit",
    "15": "15 — No Change",
    "22": "22 — Historical Record",
}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — UTILITY FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def normalize_date_edi(d: str) -> str:
    """
    Convert an X12 date string in YYYYMMDD format to a human-readable
    string like 'May 15, 1980'.

    The %-d format specifier removes the leading zero from single-digit
    days (e.g. '5' not '05').  On Windows, use '#d' instead of '%-d'.

    Falls back to returning the original string untouched if parsing fails,
    so the tool degrades gracefully with non-standard date values.
    """
    try:
        return datetime.strptime(d.strip(), "%Y%m%d").strftime("%B %-d, %Y")
    except Exception:
        # Log-worthy in production; here we just return the raw value
        return d.strip()


def normalize_ssn(s: str) -> str:
    """
    Normalise an SSN string to the standard NNN-NN-NNNN format.

    EDI files often contain SSNs as bare 9-digit strings (no dashes).
    PDFs may render them with dashes already.  Normalising both sides
    before comparison prevents false-positive mismatches.

    If the input doesn't contain exactly 9 digits, it is returned as-is
    so unusual identifiers (e.g. pseudo-SSNs, foreign IDs) are preserved.
    """
    digits = re.sub(r"\D", "", s)   # strip everything that isn't a digit
    if len(digits) == 9:
        return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
    return s  # not a standard SSN; return unchanged


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — EDI 834 PARSER
# ══════════════════════════════════════════════════════════════════════════════

def parse_edi_834(text: str) -> EnrollmentRecord:
    """
    Parse a complete raw ASC X12 834 EDI string into an EnrollmentRecord.

    THE X12 FORMAT PRIMER
    ---------------------
    An X12 EDI file is a flat string of segments separated by a terminator
    character (almost always '~').  Each segment starts with a 2-3 letter
    tag and contains elements separated by a delimiter (almost always '*').

    Example:
        NM1*IL*1*SMITH*JOHN*A**JR*34*123456789~

    Tag = NM1, elements = ['IL','1','SMITH','JOHN','A','','JR','34','123456789']
    NM1-01='IL' (insured), NM1-03='SMITH' (last), NM1-04='JOHN' (first), etc.

    DELIMITER AUTO-DETECTION
    ------------------------
    The delimiters are NOT fixed — they are declared inside the ISA segment
    itself (the first segment in every X12 file):
      - ISA01-character (position 3) is the element separator
      - The character immediately after the ISA segment body is the
        segment terminator
    We detect both dynamically so the parser works with non-standard files.

    STATEFUL PARSING
    ----------------
    The parser keeps a 'current_member' pointer that is updated each time
    an INS segment is encountered.  All subsequent member-level segments
    (NM1, DMG, HD, DTP, etc.) are attached to that current_member until
    the next INS segment resets the pointer.  This naturally handles both
    single-member and multi-member files.

    KNOWN LIMITATIONS
    -----------------
    - Only processes the first ST-SE transaction set per file
    - Only processes one HD (coverage) line per member
    - Ignores Loop 2700 (member reporting categories)
    - Does not process COB (coordination of benefits) segments
    """

    # ── Step 1: Detect element separator ─────────────────────────────────────
    # The ISA segment is the first segment in every X12 file.
    # The very first character after "ISA" is the element separator —
    # position ISA[3] in the raw text.
    isa_match = re.search(r"ISA(.)", text)
    elem_sep = isa_match.group(1) if isa_match else "*"
    # Almost always '*', but some trading partners use '|' or ':'

    # ── Step 2: Detect segment terminator ────────────────────────────────────
    # The segment terminator follows immediately after the ISA segment body.
    # The ISA segment has exactly 16 elements at fixed widths totalling
    # 105 characters, but whitespace padding and newlines make exact-position
    # detection unreliable.  A more robust method: find the "GS" segment
    # (which always immediately follows ISA) and grab the character just
    # before it — that character is the terminator.
    seg_term = "~"   # default; almost universal in production 834 files
    isa_pos = text.find("ISA")
    if isa_pos >= 0:
        gs_pos = text.find("GS", isa_pos + 100)  # GS is ~107 chars after ISA start
        if gs_pos > 0:
            # Walk backwards from GS to find the last non-whitespace character
            candidate = text[gs_pos - 3: gs_pos].strip()
            if candidate:
                seg_term = candidate[-1]
                # e.g. if text is "...000000001*0*P*:~\nGS*BE..." then candidate
                # after stripping is "~" → seg_term = "~"

    # ── Step 3: Split text into individual segments ───────────────────────────
    # Split on the segment terminator (optionally followed by whitespace /
    # newlines, since some systems add a newline after each '~').
    raw_segments = re.split(re.escape(seg_term) + r"\s*", text)

    # Build a list of element arrays, skipping empty entries
    segments = []
    for s in raw_segments:
        s = s.strip()
        if s:
            segments.append(s.split(elem_sep))
    # Each entry in 'segments' is now a list like:
    #   ['NM1', 'IL', '1', 'SMITH', 'JOHN', 'A', '', 'JR', '34', '123456789']

    # ── Step 4: Walk segments and populate the record ────────────────────────
    record = EnrollmentRecord()
    current_member: Optional[Member] = None  # tracks which member we're currently parsing
    in_subscriber = False  # True while processing the subscriber's segments

    i = 0
    while i < len(segments):
        seg = segments[i]
        tag = seg[0].strip().upper()   # normalise tag to uppercase; strip stray spaces

        # Helper: safely access element at index 'idx', returning 'default' if absent.
        # This prevents IndexError on segments with optional trailing elements.
        def e(idx, default=""):
            return seg[idx].strip() if idx < len(seg) else default

        # ── ISA: Interchange Control Header ──────────────────────────────────
        # ISA is always 16 elements.  Key elements:
        #   ISA09 = date (YYMMDD), ISA10 = time (HHMM), ISA13 = control number
        if tag == "ISA":
            record.interchange_control_num = e(13)
            raw_date = e(9)   # format: YYMMDD (2-digit year)
            raw_time = e(10)  # format: HHMM
            try:
                # strptime %y = 2-digit year (00-68 → 2000s, 69-99 → 1900s)
                record.transmission_date = datetime.strptime(raw_date, "%y%m%d").strftime("%B %-d, %Y")
            except Exception:
                record.transmission_date = raw_date
            try:
                record.transmission_time = datetime.strptime(raw_time, "%H%M").strftime("%I:%M %p ET")
            except Exception:
                record.transmission_time = raw_time

        # ── ST: Transaction Set Header ────────────────────────────────────────
        # ST01 = transaction set ID ("834"), ST02 = transaction set control number
        elif tag == "ST":
            record.transaction_set_num = e(2)  # e.g. "0001"

        # ── BGN: Beginning Segment for Benefit Enrollment ─────────────────────
        # BGN01 = transaction set purpose code (e.g. "00" = New Enrollment)
        # BGN02 = reference identification (unique file / batch ID)
        # BGN03 = date (YYYYMMDD), BGN04 = time (HHMM)
        elif tag == "BGN":
            record.file_reference_num = e(2)
            raw_bgn_date = e(3)
            raw_bgn_time = e(4)
            try:
                # BGN date uses 8-digit YYYYMMDD (unlike ISA which uses YYMMDD)
                record.effective_date = datetime.strptime(raw_bgn_date, "%Y%m%d").strftime("%B %-d, %Y")
            except Exception:
                record.effective_date = raw_bgn_date
            try:
                record.transmission_time = datetime.strptime(raw_bgn_time, "%H%M").strftime("%I:%M %p ET")
            except Exception:
                pass
            record.submission_type = BGN_TYPE_MAP.get(e(1), e(1))
            # If code not in map, fall back to the raw code string

        # ── REF*38: Group/Plan Number (header level) ──────────────────────────
        # REF qualifier "38" = Plan Number; REF02 = the actual plan/group ID
        elif tag == "REF" and e(1) == "38":
            record.group_id = e(2)

        # ── DTP*007: File Effective Date ──────────────────────────────────────
        # DTP qualifier "007" = effective date; DTP03 = date value
        # DTP02 specifies the date format: "D8" = YYYYMMDD
        elif tag == "DTP" and e(1) == "007":
            record.effective_date = normalize_date_edi(e(3))

        # ── N1: Name (Plan Sponsor and Insurance Carrier) ─────────────────────
        # The N1 segment is used in multiple loops with different qualifier codes:
        #   N1*P5 = Plan Sponsor (employer) — Loop 1000A
        #   N1*IN = Payer / Insurance Carrier — Loop 1000B
        #   N1*TV = Third-Party Administrator — Loop 1000C (we skip this)
        # N1 structure: N101=qualifier, N102=name, N103=ID type, N104=ID value
        elif tag == "N1":
            qualifier = e(1)
            name = e(2)
            id_qualifier = e(3)   # e.g. "FI" = Federal Tax ID (EIN), "XV" = Health Industry #
            identifier = e(4)
            if qualifier == "P5":
                # This is the employer/plan sponsor
                record.sponsor_name = name
                if id_qualifier == "FI":
                    # Format raw EIN digits as XX-XXXXXXX
                    digits = re.sub(r"\D", "", identifier)
                    record.sponsor_ein = f"{digits[:2]}-{digits[2:]}" if len(digits) == 9 else identifier
            elif qualifier == "IN":
                # This is the insurance carrier (payer)
                record.carrier_name = name
                record.carrier_id = identifier
            # qualifier "TV" (TPA) is intentionally ignored here

        # ── INS: Insured Benefit ──────────────────────────────────────────────
        # INS is the loop-opener for each insured member.  It signals whether
        # we are starting a subscriber (INS01='Y') or a dependent (INS01='N').
        # Key elements:
        #   INS01 = subscriber indicator ('Y' = subscriber, 'N' = dependent)
        #   INS02 = individual relationship code ('18'=self, '01'=spouse, etc.)
        #   INS03 = maintenance type code ('021'=change, '024'=cancel, etc.)
        #   INS05 = benefit status code ('A'=active, 'C'=COBRA, etc.)
        #   INS08 = employment status code ('FT'=full-time, 'PT'=part-time)
        elif tag == "INS":
            subscriber_flag = e(1)
            rel_code = e(2)
            maintenance_type = e(3)
            benefit_status = e(5)
            employment_status = e(8)

            # Create a fresh Member object for this loop
            current_member = Member()
            current_member.relationship_code = RELATIONSHIP_MAP.get(rel_code, rel_code)
            current_member.maintenance_type = MAINTENANCE_TYPE_MAP.get(maintenance_type, maintenance_type)
            current_member.benefit_status = BENEFIT_STATUS_MAP.get(benefit_status, benefit_status)
            current_member.employment_status = employment_status  # keep raw code (FT, PT, etc.)

            if subscriber_flag == "Y":
                # This loop is for the primary insured / employee
                in_subscriber = True
                current_member.role = "subscriber"
                record.subscriber = current_member
            else:
                # This loop is for a dependent (spouse, child, etc.)
                in_subscriber = False
                current_member.role = "dependent"
                record.dependents.append(current_member)
                # Note: multiple dependents are appended in order;
                # the list preserves the original sequence from the EDI file

        # ── REF (member-level): Member ID and Group Number ────────────────────
        # REF*0F = Subscriber Number / Employee ID (assigned by employer)
        # REF*1L = Group or Policy Number (can appear at member level too)
        elif tag == "REF" and current_member is not None:
            if e(1) == "0F":
                current_member.member_id = e(2)
            elif e(1) == "1L":
                # Only set group_id from member-level REF*1L if not already set
                # at the header level (REF*38 takes precedence)
                record.group_id = record.group_id or e(2)

        # ── DTP*356: Member enrollment start date (header) ───────────────────
        # This DTP (qualifier 356 = "enrollment start") is typically superseded
        # by DTP*348 inside the HD coverage loop, so we parse but don't store it
        # separately here — DTP*348 is the authoritative coverage start.
        elif tag == "DTP" and current_member is not None:
            if e(1) == "356":
                pass  # intentionally not stored; coverage start comes from DTP*348 below

        # ── NM1*IL: Insured's Name ────────────────────────────────────────────
        # NM1 qualifier "IL" = Insured or Subscriber.
        # Element positions:
        #   NM101=qualifier, NM102=entity type (1=person),
        #   NM103=last name,  NM104=first name, NM105=middle initial,
        #   NM106=name prefix, NM107=name suffix,
        #   NM108=ID qualifier ('34'=SSN), NM109=member ID / SSN
        elif tag == "NM1" and e(1) == "IL" and current_member is not None:
            current_member.last_name = e(3).title()    # 'SMITH' → 'Smith'
            current_member.first_name = e(4).title()   # 'JOHN'  → 'John'
            current_member.middle_initial = e(5) if len(seg) > 5 else None
            current_member.suffix = e(6) if len(seg) > 6 and e(6) else None
            # NM109 is the member's ID; NM108='34' means it's an SSN
            raw_ssn = e(9) if len(seg) > 9 else ""
            current_member.ssn = normalize_ssn(raw_ssn) if raw_ssn else None

        # ── PER*IP: Contact Information ────────────────────────────────────────
        # PER qualifier "IP" = Information Contact.
        # The segment uses repeating qualifier/value pairs after PER02:
        #   PER03=comm qualifier, PER04=value, PER05=qualifier, PER06=value ...
        # Common qualifiers: "TE"=telephone, "EX"=extension, "EM"=email
        elif tag == "PER" and current_member is not None:
            phone = ""
            ext = ""
            # Iterate over qualifier/value pairs (step=2) starting at element 3
            for j in range(2, len(seg) - 1, 2):
                qualifier = e(j)
                value = e(j + 1)
                if qualifier == "TE":
                    # Format raw digits as (NNN) NNN-NNNN
                    digits = re.sub(r"\D", "", value)
                    phone = f"({digits[0:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else value
                elif qualifier == "EX":
                    ext = value
            current_member.phone = f"{phone} ext. {ext}" if ext else phone

        # ── N3: Address Line(s) ───────────────────────────────────────────────
        # N301 = primary street address, N302 = secondary address (apt/suite)
        elif tag == "N3" and current_member is not None:
            current_member.address_line1 = e(1).title()  # 'APT 4B' → 'Apt 4B'
            current_member.address_line2 = e(2).title() if len(seg) > 2 and e(2) else None

        # ── N4: City/State/ZIP ────────────────────────────────────────────────
        # N401=city, N402=state (2-letter), N403=ZIP, N404=country code
        elif tag == "N4" and current_member is not None:
            current_member.city = e(1).title()   # 'SPRINGFIELD' → 'Springfield'
            current_member.state = e(2)           # keep uppercase (e.g. 'IL')
            current_member.zip_code = e(3)
            current_member.country = e(4) if len(seg) > 4 else None

        # ── DMG: Demographic Information ─────────────────────────────────────
        # DMG01 = date format qualifier (usually 'D8' = YYYYMMDD)
        # DMG02 = date of birth, DMG03 = gender code
        elif tag == "DMG" and current_member is not None:
            current_member.dob = normalize_date_edi(e(2))   # '19800515' → 'May 15, 1980'
            current_member.gender = GENDER_MAP.get(e(3), e(3))  # 'M' → 'Male'

        # ── HD: Health Coverage ───────────────────────────────────────────────
        # HD signals the start of a coverage benefit loop (Loop 2300).
        # HD01=maintenance type, HD02=unused, HD03=insurance line,
        # HD04=plan/coverage code, HD05=coverage level code
        # We create a new Coverage object each time HD is encountered.
        # If a member has multiple HD segments (e.g. health + dental),
        # only the LAST one is kept.  Extend to a list if multi-coverage needed.
        elif tag == "HD" and current_member is not None:
            cov = Coverage()
            cov.maintenance_type = MAINTENANCE_TYPE_MAP.get(e(1), e(1))
            cov.insurance_line = INSURANCE_LINE_MAP.get(e(3), e(3))   # 'HLT' → 'HLT — Health'
            cov.plan_id = e(4)                                          # e.g. 'PLAN001'
            cov.coverage_level = COVERAGE_LEVEL_MAP.get(e(5), e(5))   # 'ECV' → human label
            current_member.coverage = cov
            # DTP*348 and DTP*349 following this segment will populate start/end dates

        # ── DTP (coverage-level): Start and End Dates ─────────────────────────
        # These DTP segments appear AFTER an HD segment and belong to the
        # coverage object we just created.  We check that both current_member
        # AND its coverage exist before writing to avoid attaching coverage dates
        # to non-coverage DTP segments.
        #   DTP qualifier "348" = Coverage Start Date
        #   DTP qualifier "349" = Coverage End Date
        elif tag == "DTP" and current_member is not None and current_member.coverage is not None:
            if e(1) == "348":
                current_member.coverage.start_date = normalize_date_edi(e(3))
            elif e(1) == "349":
                raw = e(3)
                # "20991231" is the X12 convention for "no termination / open-ended"
                if raw == "20991231":
                    current_member.coverage.end_date = "December 31, 2099 (Open-ended)"
                else:
                    current_member.coverage.end_date = normalize_date_edi(raw)

        # ── REF*1W: Plan Network ID (coverage-level) ──────────────────────────
        # REF qualifier "1W" = Plan Network Identification Number.
        # Only sets plan_id if HD04 didn't already provide one (precedence: HD > REF*1W).
        elif tag == "REF" and e(1) == "1W" and current_member is not None and current_member.coverage is not None:
            current_member.coverage.plan_id = current_member.coverage.plan_id or e(2)

        # ── SE: Transaction Set Trailer ───────────────────────────────────────
        # SE01 = total segment count (must match actual count for a valid file)
        # SE02 = transaction set control number (must match ST02)
        elif tag == "SE":
            record.segment_count = e(1)

        # ── GE: Functional Group Trailer ──────────────────────────────────────
        # GE01 = number of included transaction sets (ST-SE pairs)
        elif tag == "GE":
            record.functional_group_count = e(1)

        # LS, LX, LE, IEA: structural loop delimiters — no data to extract
        # They are intentionally skipped here.

        i += 1

    return record


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — PDF PARSER
# ══════════════════════════════════════════════════════════════════════════════

def extract_pdf_text(pdf_path: str) -> str:
    """
    Extract the full text content from a PDF file using pdfplumber.

    layout=True attempts to preserve the original spatial layout of text
    by inserting spaces proportional to the gap between characters.  This
    is important for table-based PDFs where label and value appear on the
    same line, separated by whitespace rather than a delimiter character.

    All pages are extracted and joined with newlines so the entire document
    is searchable as a single string.

    REAL-WORLD NOTE: Very large PDFs (100+ pages) should be streamed page-
    by-page rather than joined into one string.  pdfplumber supports this
    with its context manager + page iteration pattern.
    """
    lines = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text(layout=True)  # layout=True preserves column spacing
            if text:
                lines.append(text)
    return "\n".join(lines)


def find_pdf_value(text: str, *labels: str) -> Optional[str]:
    """
    Search the extracted PDF text for a labelled field and return its value.

    Accepts multiple candidate label strings (e.g. "Employee ID", "Member ID")
    and returns the first match found, allowing flexibility across different
    PDF layouts that use different terminology for the same concept.

    THE TWO LAYOUT PATTERNS WE HANDLE
    ----------------------------------
    Pattern A — same-line table layout (most common in our PDFs):
        "Last Name          SMITH          First Name         JOHN"
        A label is followed by 1-6 separator characters, then the value,
        then either more whitespace (≥3 spaces), a newline, or end of line.

    Pattern B — label-on-its-own-line layout (sometimes used for long values):
        "Street Address
         123 Main Street, Apt 4B"
        The label occupies its own line; the value is on the next line.

    VALUE CLEANUP
    -------------
    In Pattern A, the regex can sometimes capture the next label as part of
    the value (e.g. "SMITH          First Name" when the right column label
    bleeds into the capture group).  We split on two or more consecutive
    spaces and take only the first token, which is the actual value.

    LIMITATIONS
    -----------
    This approach is inherently heuristic.  If your PDF uses very different
    label text, or if values contain multiple words separated by only one
    space (and followed by another label without enough whitespace padding),
    the regex may under-capture or over-capture.  See README for tuning tips.
    """
    for label in labels:
        # ── Pattern A: same-line "Label     Value" ───────────────────────────
        # Breakdown of regex:
        #   re.escape(label)          — literal label text, safely escaped
        #   [ \t:*|]{1,6}             — 1-6 separator chars (space/tab/colon/pipe)
        #   ([^\n\|]{1,60}?)          — non-greedy capture: up to 60 non-newline chars
        #   (?:\s{3,}|$|\n)           — stopped by: 3+ spaces, line-end, or newline
        pattern = re.compile(
            re.escape(label) + r"[ \t:*|]{1,6}([^\n\|]{1,60}?)(?:\s{3,}|$|\n)",
            re.IGNORECASE
        )
        m = pattern.search(text)
        if m:
            val = m.group(1).strip().strip("|").strip()
            if val and 1 < len(val) < 55:
                # Guard: split on 2+ spaces to drop any adjacent label text
                # that may have been captured (e.g. "62701  Country")
                val = re.split(r'\s{2,}', val)[0].strip()
                if val:
                    return val

        # ── Pattern B: next-line "Label\nValue" ──────────────────────────────
        pattern2 = re.compile(
            re.escape(label) + r"[ \t:]*\n[ \t]*(.+)", re.IGNORECASE
        )
        m2 = pattern2.search(text)
        if m2:
            # Again, split on 3+ spaces to avoid capturing the next column
            val = re.split(r'\s{3,}', m2.group(1).strip())[0].strip()
            if val:
                return val

    return None  # field not found in this text block


def parse_pdf_enrollment(pdf_path: str) -> EnrollmentRecord:
    """
    Extract enrollment data from a PDF enrollment summary into an
    EnrollmentRecord, using labelled field search.

    DESIGN APPROACH
    ---------------
    Rather than trying to parse the PDF's internal table structure
    (which varies by generator), we treat the extracted text as a
    flat document and search for known label strings.  This is more
    resilient to minor layout changes but requires that the PDF uses
    consistent, predictable labels.

    The labels hardcoded below correspond to the labels produced by our
    834_enrollment_summary PDF generator.  If your organisation uses a
    different PDF template, update the label strings in each
    find_pdf_value() call to match your actual PDF.

    SECTION SPLITTING
    -----------------
    Because subscriber and dependent sections use the same field labels
    (e.g. both have "Last Name", "Date of Birth"), we split the extracted
    text at the boundary between the subscriber and dependent sections
    before searching.  The split point is identified by the section header
    produced by our PDF generator ("Dependent ... Loop ... INS*N").

    If your PDF has a different section header, update the 'dep_marker'
    regex below.
    """
    text = extract_pdf_text(pdf_path)
    r = EnrollmentRecord()

    # ── Transmission metadata ─────────────────────────────────────────────────
    # These labels match section headers in our PDF template (Transmission Information table)
    r.interchange_control_num = find_pdf_value(text, "Interchange Control #")
    r.transaction_set_num     = find_pdf_value(text, "Transaction Set #")
    r.transmission_date       = find_pdf_value(text, "Transmission Date")
    r.transmission_time       = find_pdf_value(text, "Transmission Time")
    r.file_reference_num      = find_pdf_value(text, "File Reference #")
    r.submission_type         = find_pdf_value(text, "Submission Type")
    r.effective_date          = find_pdf_value(text, "Effective Date")

    # ── Plan Sponsor ──────────────────────────────────────────────────────────
    r.sponsor_name = find_pdf_value(text, "Organization Name")
    r.sponsor_ein  = find_pdf_value(text, "EIN / Tax ID")
    r.group_id     = find_pdf_value(text, "Group / Plan ID")

    # ── Insurance Carrier ─────────────────────────────────────────────────────
    r.carrier_name = find_pdf_value(text, "Carrier Name")
    r.carrier_id   = find_pdf_value(text, "Carrier ID")

    # ── Split text into subscriber vs. dependent sections ─────────────────────
    # Search for the section divider between subscriber and dependent blocks.
    # The regex matches our PDF template's dependent section header, which
    # contains "Dependent" followed by loop info and "INS*N".
    # CUSTOMISE THIS PATTERN if your PDF uses different section headers.
    sub_section = text   # default: entire text is the subscriber section
    dep_section = ""
    dep_marker = re.search(
        r"Dependent\s*[\(/].*?Loop|Dependent.*?INS\*N",
        text,
        re.IGNORECASE
    )
    if dep_marker:
        # Everything before the dependent header belongs to the subscriber
        sub_section = text[:dep_marker.start()]
        # Everything from the header onwards is the dependent section
        dep_section  = text[dep_marker.start():]

    # ── Inner helper: parse a member block from a text slice ─────────────────
    def parse_member_from_text(t: str, role: str) -> Member:
        """
        Extract all member-level fields from a text slice that corresponds
        to one insured person (subscriber or dependent).

        Called twice: once with sub_section for the subscriber, once with
        dep_section for a single dependent.  For multiple dependents, this
        would need to be extended to split dep_section further (by additional
        INS*N section headers).  See README for guidance.
        """
        m = Member(role=role)

        # Personal identity fields
        m.last_name    = find_pdf_value(t, "Last Name")
        m.first_name   = find_pdf_value(t, "First Name")
        m.middle_initial = find_pdf_value(t, "Middle Initial")
        m.suffix       = find_pdf_value(t, "Suffix")

        # SSN: normalise so both "123-45-6789" and "123456789" compare equal
        raw_ssn = find_pdf_value(t, "SSN")
        m.ssn = normalize_ssn(raw_ssn) if raw_ssn else None

        # ID: try both label variants ("Employee ID" for subscriber, "Dependent ID" for dep)
        m.member_id = find_pdf_value(t, "Employee ID", "Dependent ID")

        # Demographics
        m.dob    = find_pdf_value(t, "Date of Birth")
        m.gender = find_pdf_value(t, "Gender")

        # Enrollment metadata
        m.relationship_code = find_pdf_value(t, "Relationship Code")
        m.benefit_status    = find_pdf_value(t, "Benefit Status")
        m.maintenance_type  = find_pdf_value(t, "Maintenance Type")
        m.employment_status = find_pdf_value(t, "Employment Status")

        # Address (meaningful for subscriber; often absent for dependents)
        m.address_line1 = find_pdf_value(t, "Street Address")
        m.city          = find_pdf_value(t, "City")
        m.state         = find_pdf_value(t, "State")
        m.zip_code      = find_pdf_value(t, "ZIP Code")
        m.country       = find_pdf_value(t, "Country")
        m.phone         = find_pdf_value(t, "Phone")

        # Coverage — try to extract from the same text slice
        cov = Coverage()
        cov.start_date     = find_pdf_value(t, "Coverage Start Date")
        cov.end_date       = find_pdf_value(t, "Coverage End Date")
        cov.plan_id        = find_pdf_value(t, "Plan ID")
        cov.coverage_level = find_pdf_value(t, "Coverage Level")
        cov.insurance_line = find_pdf_value(t, "Insurance Line")
        cov.maintenance_type = find_pdf_value(t, "Maintenance Type")

        # Only attach a Coverage object if at least one coverage field was found;
        # avoids attaching empty Coverage objects to members with no coverage info
        if any([cov.start_date, cov.end_date, cov.plan_id]):
            m.coverage = cov

        return m

    # Parse the subscriber from the subscriber-only slice of text
    r.subscriber = parse_member_from_text(sub_section, "subscriber")

    # Parse one dependent (if a dependent section was found)
    if dep_section:
        r.dependents.append(parse_member_from_text(dep_section, "dependent"))
    # NOTE: For PDFs with multiple dependents, split dep_section further
    # by additional section headers before calling parse_member_from_text()

    # Trailer fields (from the Transaction Trailer section of the PDF)
    r.segment_count           = find_pdf_value(text, "Total Segment Count")
    r.functional_group_count  = find_pdf_value(text, "Functional Groups")

    return r


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — DIFF ENGINE
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Discrepancy:
    """
    Represents a single field-level mismatch between the EDI and PDF records.

    Severity levels:
      ERROR   — Both values were found but they don't match.
                This is a genuine data quality issue requiring investigation.
      WARNING — One side is missing / not extracted.
                May indicate a PDF layout issue rather than a true data error.
      INFO    — Informational difference (not currently used, reserved for
                future use e.g. case-only differences, whitespace differences).

    The __str__ method formats the discrepancy for the text report.
    """
    section: str              # logical grouping, e.g. "Subscriber", "Plan Sponsor"
    field: str                # field name, e.g. "Last Name", "ZIP Code"
    edi_value: Optional[str]  # value extracted from the EDI file (None = not found)
    pdf_value: Optional[str]  # value extracted from the PDF    (None = not found)
    severity: str             # "ERROR" | "WARNING" | "INFO"

    def __str__(self):
        """Render as a formatted two-line string for the report."""
        sev_icon = {"ERROR": "✗", "WARNING": "⚠", "INFO": "ℹ"}.get(self.severity, "?")
        return (
            f"  [{self.severity}] {sev_icon} {self.section} > {self.field}\n"
            f"       EDI : {self.edi_value or '(not set)'}\n"
            f"       PDF : {self.pdf_value or '(not set)'}"
        )


def normalize_for_compare(v: Optional[str]) -> str:
    """
    Produce a normalised comparison key from a string by:
      1. Returning empty string for None/empty values
      2. Removing spaces, hyphens, parentheses, dots, commas, and slashes
      3. Converting to uppercase

    This allows 'Smith' == 'SMITH', '123-45-6789' == '123456789',
    '(555) 123-4567' == '5551234567', etc.

    The goal is to catch genuine data differences while ignoring trivial
    formatting differences that arise from EDI → PDF rendering.
    """
    if v is None:
        return ""
    return re.sub(r"[\s\-\(\)\.\,\/]", "", v).upper()


def values_match(a: Optional[str], b: Optional[str]) -> bool:
    """
    Determine whether two field values are 'effectively equal' for
    reconciliation purposes.

    Two checks are applied in order:

    1. EXACT MATCH (after normalisation):
       Both sides reduce to identical strings.  Handles formatting
       differences (case, dashes, spaces).

    2. SUBSTRING MATCH:
       One side is a substring of the other.  Handles situations where the
       PDF renders a full description ("ECV — Employee + Children + Spouse")
       while the EDI stores the raw code ("ECV").  The normalised forms would
       be "ECV" and "ECVEMPLOYEECHILDRENSPOUSE", and the former is in the
       latter, so this is treated as a match.

    Note: substring matching can produce false-negatives for very short
    strings (e.g. "IL" would match any state whose code contains "IL").
    In practice this has not been an issue for the 834 fields we compare,
    but be mindful if you add short-code fields.
    """
    na, nb = normalize_for_compare(a), normalize_for_compare(b)
    if na == nb:
        return True   # exact match after normalisation
    # Substring match (handles code vs. full-label pairs)
    if na and nb and (na in nb or nb in na):
        return True
    return False


def diff_field(section: str, field: str,
               edi_val: Optional[str], pdf_val: Optional[str],
               severity: str = "ERROR") -> Optional[Discrepancy]:
    """
    Compare a single field from the EDI and PDF records.

    Returns a Discrepancy if the values don't match, or None if they do.

    Severity auto-downgrade:
      If the default severity is ERROR but one side is missing (None / empty),
      the severity is automatically downgraded to WARNING because the mismatch
      is likely a PDF extraction issue rather than a genuine data error.
    """
    if not values_match(edi_val, pdf_val):
        # If only one side is missing, it's more likely an extraction issue
        # than a genuine data discrepancy → downgrade to WARNING
        if not edi_val or not pdf_val:
            severity = "WARNING"
        return Discrepancy(section, field, edi_val, pdf_val, severity)
    return None  # values match; no discrepancy


def diff_records(edi: EnrollmentRecord, pdf: EnrollmentRecord) -> list[Discrepancy]:
    """
    Compare every field in two EnrollmentRecord objects and return a list
    of all Discrepancy objects found.

    ORGANISATION
    ------------
    Fields are checked in logical groups that mirror the 834 loop structure:
      1. Transmission metadata
      2. Plan Sponsor
      3. Insurance Carrier
      4. Subscriber (with nested Coverage)
      5. Dependents (with nested Coverage, one per dependent)
      6. Transaction Trailer

    FIELD SEVERITY POLICY
    ---------------------
    - Name fields (Last Name, First Name): ERROR — critical for member matching
    - SSN, DOB, Member ID: ERROR — PII/identity fields; mismatches are serious
    - Address fields (ZIP, City, State): ERROR — affects claims routing
    - Middle Initial, Suffix: WARNING — often missing from PDFs; low risk
    - Phone, Maintenance Type: WARNING — informational; minor impact
    - Segment Count: WARNING — useful sanity check but not clinically critical
    """
    diffs = []

    # Convenience wrapper: creates a Discrepancy if values differ and appends it
    def check(section: str, field: str,
              e_val: Optional[str], p_val: Optional[str],
              severity: str = "ERROR"):
        d = diff_field(section, field, e_val, p_val, severity)
        if d:
            diffs.append(d)

    # ── 1. Transmission metadata ──────────────────────────────────────────────
    check("Transmission", "Interchange Control #", edi.interchange_control_num, pdf.interchange_control_num)
    check("Transmission", "Transaction Set #",     edi.transaction_set_num,     pdf.transaction_set_num)
    check("Transmission", "Transmission Date",     edi.transmission_date,       pdf.transmission_date)
    check("Transmission", "File Reference #",      edi.file_reference_num,      pdf.file_reference_num)
    check("Transmission", "Submission Type",       edi.submission_type,         pdf.submission_type)
    check("Transmission", "Effective Date",        edi.effective_date,          pdf.effective_date)

    # ── 2. Plan Sponsor ───────────────────────────────────────────────────────
    check("Plan Sponsor", "Organization Name", edi.sponsor_name, pdf.sponsor_name)
    check("Plan Sponsor", "EIN / Tax ID",      edi.sponsor_ein,  pdf.sponsor_ein)
    check("Plan Sponsor", "Group / Plan ID",   edi.group_id,     pdf.group_id)

    # ── 3. Insurance Carrier ──────────────────────────────────────────────────
    check("Insurance Carrier", "Carrier Name", edi.carrier_name, pdf.carrier_name)
    check("Insurance Carrier", "Carrier ID",   edi.carrier_id,   pdf.carrier_id)

    # ── 4. Member diff (reusable for both subscriber and dependents) ──────────
    def diff_member(section_prefix: str,
                    edi_m: Optional[Member],
                    pdf_m: Optional[Member]):
        """
        Diff all fields of one Member object and its nested Coverage.

        section_prefix is used as the report section label, e.g.
        "Subscriber", "Dependent #1", etc.

        Edge cases:
          - If EDI has the member but PDF doesn't (or vice versa),
            record the entire member as missing rather than individual fields.
          - Address fields are only checked for the subscriber because
            dependent address is typically not printed on PDFs.
          - Coverage sub-section is nested as "<prefix> > Coverage".
        """
        # Handle entire-member missing scenarios
        if edi_m is None and pdf_m is None:
            return  # nothing to compare
        if edi_m is None:
            diffs.append(Discrepancy(section_prefix, "(entire member)",
                                     None, "Present in PDF only", "ERROR"))
            return
        if pdf_m is None:
            diffs.append(Discrepancy(section_prefix, "(entire member)",
                                     "Present in EDI only", None, "ERROR"))
            return

        # Personal identity — ERROR severity (PII fields)
        check(section_prefix, "Last Name",         edi_m.last_name,   pdf_m.last_name)
        check(section_prefix, "First Name",        edi_m.first_name,  pdf_m.first_name)
        check(section_prefix, "SSN",               edi_m.ssn,         pdf_m.ssn)
        check(section_prefix, "Member / Employee ID", edi_m.member_id, pdf_m.member_id)
        check(section_prefix, "Date of Birth",     edi_m.dob,         pdf_m.dob)
        check(section_prefix, "Gender",            edi_m.gender,      pdf_m.gender)
        check(section_prefix, "Benefit Status",    edi_m.benefit_status, pdf_m.benefit_status)

        # Lower-risk fields — WARNING severity
        check(section_prefix, "Middle Initial",    edi_m.middle_initial,    pdf_m.middle_initial, "WARNING")
        check(section_prefix, "Suffix",            edi_m.suffix,            pdf_m.suffix,         "WARNING")
        check(section_prefix, "Relationship Code", edi_m.relationship_code, pdf_m.relationship_code, "WARNING")
        check(section_prefix, "Maintenance Type",  edi_m.maintenance_type,  pdf_m.maintenance_type,  "WARNING")

        # Address fields — only compare for subscriber (dependents share address)
        if edi_m.role == "subscriber":
            check(section_prefix, "Street Address", edi_m.address_line1, pdf_m.address_line1)
            check(section_prefix, "City",           edi_m.city,          pdf_m.city)
            check(section_prefix, "State",          edi_m.state,         pdf_m.state)
            check(section_prefix, "ZIP Code",       edi_m.zip_code,      pdf_m.zip_code)
            check(section_prefix, "Phone",          edi_m.phone,         pdf_m.phone, "WARNING")

        # Coverage sub-section
        edi_c, pdf_c = edi_m.coverage, pdf_m.coverage
        cov_section = f"{section_prefix} > Coverage"
        if edi_c or pdf_c:
            # Create empty Coverage objects for any None side so we can still
            # diff field-by-field (produces WARNINGs for missing coverage data)
            edi_c = edi_c or Coverage()
            pdf_c = pdf_c or Coverage()
            check(cov_section, "Plan ID",             edi_c.plan_id,        pdf_c.plan_id)
            check(cov_section, "Coverage Start Date", edi_c.start_date,     pdf_c.start_date)
            check(cov_section, "Coverage End Date",   edi_c.end_date,       pdf_c.end_date)
            check(cov_section, "Coverage Level",      edi_c.coverage_level, pdf_c.coverage_level)
            check(cov_section, "Insurance Line",      edi_c.insurance_line, pdf_c.insurance_line, "WARNING")

    # Run the member diff for the subscriber
    diff_member("Subscriber", edi.subscriber, pdf.subscriber)

    # ── 5. Dependents ─────────────────────────────────────────────────────────
    # Zip together EDI and PDF dependent lists by position (Dependent #1, #2, …).
    # If counts differ, one side will supply None — diff_member handles that.
    edi_deps = edi.dependents
    pdf_deps = pdf.dependents
    max_deps = max(len(edi_deps), len(pdf_deps)) if (edi_deps or pdf_deps) else 0
    for idx in range(max_deps):
        label    = f"Dependent #{idx + 1}"
        edi_dep  = edi_deps[idx] if idx < len(edi_deps) else None
        pdf_dep  = pdf_deps[idx] if idx < len(pdf_deps) else None
        diff_member(label, edi_dep, pdf_dep)

    # ── 6. Transaction Trailer ────────────────────────────────────────────────
    # SE01 segment count is a self-consistency check; WARNING only
    check("Transaction Trailer", "Segment Count",
          edi.segment_count, pdf.segment_count, "WARNING")

    return diffs


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — REPORT FORMATTER
# ══════════════════════════════════════════════════════════════════════════════

def format_report(edi: EnrollmentRecord,
                  pdf: EnrollmentRecord,
                  diffs: list,
                  edi_source: str,
                  pdf_source: str) -> str:
    """
    Render the reconciliation results as a human-readable plain-text report.

    REPORT STRUCTURE
    ----------------
    1. Header banner — file paths, timestamp, overall PASS / FAIL status
    2. Discrepancy detail — one block per affected section, sorted by section
       Each block lists individual field mismatches with EDI and PDF values
    3. Field comparison summary table — one row per critical field showing
       short EDI value, short PDF value, and a ✓/✗ match indicator

    OUTPUT FORMATS
    --------------
    The report is returned as a plain string.  The caller decides whether
    to print it or write it to a file.  To support other output formats
    (HTML, CSV, JSON) in the future, this function could be refactored into
    a base class with format-specific subclasses.
    """
    width    = 72          # total width of report separators
    sep      = "─" * width
    thick_sep = "═" * width

    lines = []
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Header ────────────────────────────────────────────────────────────────
    lines.append(thick_sep)
    lines.append("  834 EDI ↔ PDF RECONCILIATION REPORT")
    lines.append(f"  Generated : {ts}")
    lines.append(f"  EDI File  : {edi_source}")
    lines.append(f"  PDF File  : {pdf_source}")
    lines.append(thick_sep)
    lines.append("")

    # ── Summary ───────────────────────────────────────────────────────────────
    errors   = [d for d in diffs if d.severity == "ERROR"]
    warnings = [d for d in diffs if d.severity == "WARNING"]
    infos    = [d for d in diffs if d.severity == "INFO"]

    status = ("✓  PASS — No discrepancies found."
              if not diffs else
              f"✗  FAIL — {len(diffs)} discrepancy/ies found.")
    lines.append(f"  RESULT  : {status}")
    lines.append(f"  Errors  : {len(errors)}")
    lines.append(f"  Warnings: {len(warnings)}")
    lines.append(f"  Info    : {len(infos)}")
    lines.append("")
    lines.append(sep)

    # ── Discrepancy Detail ────────────────────────────────────────────────────
    if not diffs:
        lines.append("")
        lines.append("  All checked fields match between the EDI file and the PDF.")
        lines.append("")
    else:
        # Group discrepancies by their section for readability
        sections: dict[str, list[Discrepancy]] = {}
        for d in diffs:
            sections.setdefault(d.section, []).append(d)

        for section, section_diffs in sections.items():
            lines.append("")
            lines.append(f"  SECTION: {section}")
            lines.append("  " + "·" * (width - 2))
            for d in section_diffs:
                lines.append(str(d))  # calls Discrepancy.__str__()
            lines.append("")

    # ── Field Comparison Summary Table ────────────────────────────────────────
    lines.append(sep)
    lines.append("")
    lines.append("  FIELD COMPARISON SUMMARY")
    lines.append("  " + "·" * (width - 2))

    # Build a flat list of (field_label, edi_value, pdf_value, matched) tuples
    all_checks = []

    def summarize_member(prefix: str, edi_m: Optional[Member], pdf_m: Optional[Member]):
        """Add key fields from one member pair to the summary table."""
        fields = [
            # (display_name, getter_function)
            ("Last Name",      lambda m: m.last_name),
            ("First Name",     lambda m: m.first_name),
            ("SSN",            lambda m: m.ssn),
            ("Date of Birth",  lambda m: m.dob),
            ("Gender",         lambda m: m.gender),
            # Coverage fields accessed through nested Coverage object
            ("Coverage Start", lambda m: m.coverage.start_date  if m and m.coverage else None),
            ("Coverage End",   lambda m: m.coverage.end_date    if m and m.coverage else None),
            ("Plan ID",        lambda m: m.coverage.plan_id     if m and m.coverage else None),
        ]
        for fname, getter in fields:
            ev = getter(edi_m) if edi_m else None
            pv = getter(pdf_m) if pdf_m else None
            all_checks.append((f"{prefix} > {fname}", ev, pv, values_match(ev, pv)))

    summarize_member("Subscriber", edi.subscriber, pdf.subscriber)
    for idx, (ed, pd) in enumerate(zip(edi.dependents, pdf.dependents)):
        summarize_member(f"Dependent #{idx+1}", ed, pd)

    # Print the summary table with fixed-width columns
    col1, col2, col3, col4 = 30, 20, 20, 6
    header = f"  {'Field':<{col1}} {'EDI Value':<{col2}} {'PDF Value':<{col3}} {'Match':<{col4}}"
    lines.append(header)
    lines.append("  " + "-" * (col1 + col2 + col3 + col4 + 3))
    for fname, ev, pv, match in all_checks:
        # Truncate long values to fit column width
        ev_short   = (ev or "(none)")[:col2 - 1]
        pv_short   = (pv or "(none)")[:col3 - 1]
        match_icon = "✓" if match else "✗"
        lines.append(f"  {fname:<{col1}} {ev_short:<{col2}} {pv_short:<{col3}} {match_icon}")

    lines.append("")
    lines.append(thick_sep)
    lines.append("  END OF REPORT")
    lines.append(thick_sep)

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — BUILT-IN SAMPLE DATA (for --demo mode)
# ══════════════════════════════════════════════════════════════════════════════
#
# A complete, realistic 834 EDI file for a subscriber (John Smith) and one
# dependent (Jane Smith, spouse).  Conforms to ASC X12 005010X220A1.
#
# Used by --demo to allow testing without needing real files.
# Used by --demo --inject-errors to demonstrate the diff engine catching
# three deliberate data quality issues.
# ──────────────────────────────────────────────────────────────────────────────

SAMPLE_EDI = """ISA*00*          *00*          *ZZ*EMPLOYER        *ZZ*HEALTHPLAN      *230301*1200*^*00501*000000001*0*P*:~
GS*BE*EMPLOYER*HEALTHPLAN*20230301*1200*1*X*005010X220A1~
ST*834*0001*005010X220A1~
BGN*00*REF12345*20230301*1200*ET**2~
REF*38*GROUPID001~
DTP*007*D8*20230301~
N1*P5*ACME CORPORATION*FI*123456789~
N1*IN*BLUE CROSS BLUE SHIELD*XV*987654321~
N1*TV*BLUE CROSS BLUE SHIELD*XV*987654321~
INS*Y*18*021*28*A*FT*E**FT~
REF*0F*EMP001~
REF*1L*GROUP001~
DTP*356*D8*20230301~
NM1*IL*1*SMITH*JOHN*A**JR*34*123456789~
PER*IP**TE*5551234567*EX*101~
N3*123 MAIN STREET*APT 4B~
N4*SPRINGFIELD*IL*62701*US~
DMG*D8*19800515*M~
HD*021**HLT*PLAN001*ECV~
DTP*348*D8*20230301~
DTP*349*D8*20991231~
REF*1W*PLANID001~
LS*2700~
LX*1~
N1*P5*ACME CORPORATION~
LE*2700~
INS*N*01*021*28*A*FT*E**FT~
REF*0F*DEP001~
DTP*356*D8*20230301~
NM1*IL*1*SMITH*JANE***  *34*987654321~
DMG*D8*19820820*F~
HD*021**HLT*PLAN001*ECV~
DTP*348*D8*20230301~
DTP*349*D8*20991231~
LS*2700~
LX*1~
N1*P5*ACME CORPORATION~
LE*2700~
SE*34*0001~
GE*1*1~
IEA*1*000000001~"""

# Error-injected variant — three deliberate mistakes to test the diff engine:
#   1. Last name typo:  SMITH  → SMYTH  (NM1 segment)
#   2. Wrong DOB month: 05     → 06     (DMG segment: May → June)
#   3. Wrong ZIP:       62701  → 62702  (N4 segment)
SAMPLE_EDI_WITH_ERRORS = SAMPLE_EDI.replace(
    "NM1*IL*1*SMITH*JOHN*A**JR*34*123456789",
    "NM1*IL*1*SMYTH*JOHN*A**JR*34*123456789"   # typo in last name
).replace(
    "DMG*D8*19800515*M",
    "DMG*D8*19800615*M"                         # June (06) instead of May (05)
).replace(
    "N4*SPRINGFIELD*IL*62701*US",
    "N4*SPRINGFIELD*IL*62702*US"                # wrong ZIP code
)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — CLI ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    """
    Parse CLI arguments, orchestrate the reconciliation workflow, and
    print or save the report.

    WORKFLOW
    --------
    Real files:
      1. Read EDI file from disk
      2. Parse EDI → EnrollmentRecord
      3. Parse PDF → EnrollmentRecord
      4. Diff the two records
      5. Format report
      6. Print to stdout or write to --output file

    Demo mode:
      Same as above but uses SAMPLE_EDI (or SAMPLE_EDI_WITH_ERRORS)
      as the EDI source and an existing 834_enrollment_summary.pdf as
      the PDF source.  No real files required.
    """
    parser = argparse.ArgumentParser(
        description="Diff an 834 EDI file against its PDF enrollment summary.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        Examples:
          python edi834_diff.py --edi enrollment.834 --pdf enrollment_summary.pdf
          python edi834_diff.py --edi enrollment.834 --pdf enrollment_summary.pdf --output report.txt
          python edi834_diff.py --demo
          python edi834_diff.py --demo --inject-errors
        """)
    )
    parser.add_argument("--edi",
        help="Path to the 834 EDI file (plain text, .edi or .834 extension)")
    parser.add_argument("--pdf",
        help="Path to the corresponding PDF enrollment summary")
    parser.add_argument("--output",
        help="Optional path to save the report (default: print to stdout)")
    parser.add_argument("--demo",
        action="store_true",
        help="Run against built-in sample data; requires 834_enrollment_summary.pdf in same directory or outputs dir")
    parser.add_argument("--inject-errors",
        action="store_true",
        help="Used with --demo: uses error-injected EDI to demonstrate discrepancy detection")

    args = parser.parse_args()

    if args.demo:
        import os

        # Locate the companion PDF (generated by generate_834_pdf.py)
        # Check the outputs directory first, then the script's own directory
        styled_pdf = "/mnt/user-data/outputs/834_enrollment_summary.pdf"
        if not os.path.exists(styled_pdf):
            styled_pdf = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "834_enrollment_summary.pdf"
            )
        if not os.path.exists(styled_pdf):
            print(
                "ERROR: --demo requires 834_enrollment_summary.pdf.\n"
                "Run generate_834_pdf.py first to create it, then re-run this script."
            )
            sys.exit(1)

        # Select clean or error-injected EDI based on flag
        edi_text = SAMPLE_EDI_WITH_ERRORS if args.inject_errors else SAMPLE_EDI
        edi_source = (
            "built-in sample (with 3 injected errors: last name SMITH→SMYTH, "
            "DOB May→June, ZIP 62701→62702)"
            if args.inject_errors else
            "built-in sample (clean)"
        )
        pdf_source = styled_pdf

        # Run the reconciliation
        edi_record = parse_edi_834(edi_text)
        pdf_record = parse_pdf_enrollment(styled_pdf)
        diffs      = diff_records(edi_record, pdf_record)
        report     = format_report(edi_record, pdf_record, diffs, edi_source, pdf_source)

    else:
        # Real-file mode
        if not args.edi or not args.pdf:
            parser.error("--edi and --pdf are both required (or use --demo for a quick test)")

        # Read EDI as text; errors='replace' handles files with unusual encoding
        with open(args.edi, "r", errors="replace") as f:
            edi_text = f.read()

        edi_record = parse_edi_834(edi_text)
        pdf_record = parse_pdf_enrollment(args.pdf)
        diffs      = diff_records(edi_record, pdf_record)
        report     = format_report(edi_record, pdf_record, diffs, args.edi, args.pdf)

    # ── Output ────────────────────────────────────────────────────────────────
    if args.output and not args.demo:
        # Save report to file if --output is specified (not available in demo mode)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"Report saved to: {args.output}")
    else:
        # Default: print to stdout
        print(report)


# Standard Python entry point guard — allows this file to be imported as
# a module (e.g. in unit tests) without immediately running main()
if __name__ == "__main__":
    main()
