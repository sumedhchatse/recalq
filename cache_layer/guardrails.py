"""
guardrails.py — PII detection and redaction for Recalq
Place this file at: ~/memlayer/cache_layer/guardrails.py

Scans text for sensitive patterns common in Indian BFSI context.
Two modes:
  - BLOCK mode: refuse the query entirely
  - REDACT mode: mask the PII and continue processing (default)
"""
import re
import logging

log = logging.getLogger("recalq.guardrails")

# ── Regex patterns — ORDER MATTERS, most specific first ───────
# We check in this priority order so a number isn't double-matched
# by a looser pattern (e.g. account number context vs bare aadhaar).

PATTERN_ORDER = [
    # Secrets first — scan_text() claims spans in order, so a token gets
    # claimed as a secret before a looser numeric pattern can grab part of it.
    "private_key_block",
    "jwt",
    "google_api_key",
    "google_api_key_v2",
    "groq_api_key",
    "anthropic_api_key",
    "openai_api_key",
    "nvidia_api_key",
    "github_token",
    "pypi_token",
    "slack_token",
    "aws_access_key",
    "env_secret_assignment",
    "credit_card",            # most specific: 16-digit + luhn
    "ifsc",                   # very specific format
    "pan",                    # very specific format
    "cvv_context",            # keyword-anchored
    "account_number_context", # keyword-anchored
    "email",
    "aadhaar",                # generic 12-digit, checked LAST among numerics
    "indian_mobile",
]

PATTERNS = {
    # ── API keys / tokens ─────────────────────────────────────────
    # Vendor-specific prefixes: high precision, safe on live queries.
    # Google issues two key formats. Older AI Studio keys are AIza + 35.
    # Newer ones look like AQ.<~50 alphanumerics> — a real key on this box
    # is 53 chars, "AQ." plus alphanumerics only. Both are kept: old keys
    # still appear in older chat history.
    "google_api_key": re.compile(
        r"AIza[0-9A-Za-z_\-]{35}"
    ),
    "google_api_key_v2": re.compile(
        r"\bAQ\.[0-9A-Za-z]{40,60}\b"
    ),
    "groq_api_key": re.compile(
        r"\bgsk_[0-9A-Za-z]{20,}\b"
    ),
    "anthropic_api_key": re.compile(
        r"\bsk-ant-[0-9A-Za-z_\-]{20,}\b"
    ),
    "openai_api_key": re.compile(
        r"\bsk-(?:proj-)?[0-9A-Za-z_\-]{20,}\b"
    ),
    "nvidia_api_key": re.compile(
        r"\bnvapi-[0-9A-Za-z_\-]{20,}\b"
    ),
    "github_token": re.compile(
        r"\bgh[pousr]_[0-9A-Za-z]{20,}\b"
    ),
    "pypi_token": re.compile(
        r"\bpypi-[0-9A-Za-z_\-]{30,}\b"
    ),
    "slack_token": re.compile(
        r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b"
    ),
    "aws_access_key": re.compile(
        r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    ),
    "private_key_block": re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"
    ),
    "jwt": re.compile(
        r"\beyJ[0-9A-Za-z_\-]{10,}\.[0-9A-Za-z_\-]{10,}\.[0-9A-Za-z_\-]{10,}\b"
    ),
    # Quoted .env-style secret assignment. Scoped to secret-ish key names AND
    # a quoted value, so ordinary prose about passwords isn't caught.
    "env_secret_assignment": re.compile(
        r"\b[A-Z][A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|API_KEY|APIKEY|PRIVATE_KEY)\s*=\s*[\"'][^\"'\n]{6,}[\"']"
    ),

    "credit_card": re.compile(
        r'\b(?:\d{4}[\s-]?){3}\d{4}\b'
    ),
    "ifsc": re.compile(
        r'\b[A-Z]{4}0[A-Z0-9]{6}\b'
    ),
    "pan": re.compile(
        r'\b[A-Z]{5}[0-9]{4}[A-Z]{1}\b'
    ),
    "cvv_context": re.compile(
        r'\b(?:cvv|cvc)\s*(?:is|:|-)?\s*(\d{3,4})\b',
        re.IGNORECASE
    ),
    "account_number_context": re.compile(
        r'\b(?:account|a/c|acc)\s*(?:no|number|num)?\.?\s*(?:is|:|-)?\s*(\d{9,18})\b',
        re.IGNORECASE
    ),
    "email": re.compile(
        r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'
    ),
    "aadhaar": re.compile(
        r'\b\d{4}\s?\d{4}\s?\d{4}\b'
    ),
    "indian_mobile": re.compile(
        r'\b(?:\+91[\-\s]?)?[6-9]\d{9}\b'
    ),
}

SEVERITY = {
    "private_key_block":         "critical",
    "jwt":                       "critical",
    "google_api_key":            "critical",
    "google_api_key_v2":         "critical",
    "groq_api_key":              "critical",
    "anthropic_api_key":         "critical",
    "openai_api_key":            "critical",
    "nvidia_api_key":            "critical",
    "github_token":              "critical",
    "pypi_token":                "critical",
    "slack_token":               "critical",
    "aws_access_key":            "critical",
    "env_secret_assignment":     "critical",

    "aadhaar":                "critical",
    "credit_card":             "critical",
    "cvv_context":             "critical",
    "account_number_context":  "high",
    "pan":                     "high",
    "ifsc":                    "medium",
    "indian_mobile":           "low",
    "email":                   "low",
}

CRITICAL_TYPES = {k for k, v in SEVERITY.items() if v == "critical"}
HIGH_TYPES     = {k for k, v in SEVERITY.items() if v == "high"}


def luhn_check(number: str) -> bool:
    digits = [int(d) for d in re.sub(r'\D', '', number)]
    if len(digits) < 13:
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def scan_text(text: str) -> list:
    """
    Scan text for PII patterns in priority order.
    Once a span of text is claimed by a higher-priority pattern,
    lower-priority patterns cannot re-match overlapping characters.
    """
    findings = []
    claimed_spans = []  # list of (start, end) already matched

    def overlaps(start, end):
        for cs, ce in claimed_spans:
            if start < ce and end > cs:
                return True
        return False

    for pii_type in PATTERN_ORDER:
        pattern = PATTERNS[pii_type]
        for m in pattern.finditer(text):
            matched = m.group(0)
            start, end = m.start(), m.end()

            if overlaps(start, end):
                continue

            if pii_type == "credit_card":
                if not luhn_check(matched):
                    continue
            if pii_type == "aadhaar":
                digits_only = re.sub(r'\D', '', matched)
                if len(digits_only) != 12:
                    continue

            findings.append({
                "type":     pii_type,
                "match":    matched,
                "severity": SEVERITY.get(pii_type, "low"),
                "start":    start,
                "end":      end,
            })
            claimed_spans.append((start, end))

    findings.sort(key=lambda f: f["start"])
    return findings


def redact_text(text: str, findings: list = None) -> str:
    if findings is None:
        findings = scan_text(text)

    findings_sorted = sorted(findings, key=lambda f: f["start"], reverse=True)

    redacted = text
    for f in findings_sorted:
        placeholder = "xxxxxx"
        redacted = redacted[:f["start"]] + placeholder + redacted[f["end"]:]

    return redacted


def check_query(text: str, mode: str = "redact") -> dict:
    """
    Main entry point — call on every incoming user query
    BEFORE it reaches the cache layer or any LLM provider.
    mode: "block" | "redact"
    """
    findings = scan_text(text)

    if not findings:
        return {
            "safe": True, "action": "allow", "findings": [],
            "clean_text": text, "blocked_message": None
        }

    has_critical = any(f["type"] in CRITICAL_TYPES for f in findings)
    has_high     = any(f["type"] in HIGH_TYPES for f in findings)

    log.warning(
        f"PII detected | types={[f['type'] for f in findings]} | "
        f"critical={has_critical} | mode={mode}"
    )

    if mode == "block" and (has_critical or has_high):
        types_found = ", ".join(sorted(set(f["type"] for f in findings)))
        return {
            "safe": False, "action": "block", "findings": findings,
            "clean_text": None,
            "blocked_message": (
                f"This query appears to contain sensitive personal information "
                f"({types_found}). For your security, this has been blocked "
                f"before reaching any AI provider. Please rephrase without "
                f"including this information."
            )
        }

    clean = redact_text(text, findings)
    return {
        "safe": False, "action": "redact", "findings": findings,
        "clean_text": clean, "blocked_message": None
    }


def check_answer(text: str) -> dict:
    """Scan LLM responses before caching/displaying. Always redact mode."""
    findings = scan_text(text)
    if not findings:
        return {"safe": True, "findings": [], "clean_text": text}

    clean = redact_text(text, findings)
    log.warning(f"PII found in LLM answer, redacted | types={[f['type'] for f in findings]}")
    return {"safe": False, "findings": findings, "clean_text": clean}


if __name__ == "__main__":
    tests = [
        "what is kubernetes",
        "my aadhaar is 1234 5678 9012 please verify",
        "my PAN number is ABCDE1234F",
        "card number 4532015112830366 expiry 12/26",
        "call me at 9876543210",
        "my account number is 123456789012",
        "the cvv is 123",
        "contact me at test@example.com",
        "IFSC code HDFC0001234",
        "compare docker and kubernetes",
    ]
    print("=" * 70)
    for t in tests:
        result = check_query(t, mode="redact")
        status = "CLEAN" if result["safe"] else f"FLAGGED ({result['action']})"
        print(f"[{status:20}] {t}")
        if not result["safe"]:
            print(f"    -> {result['clean_text']}")
    print("=" * 70)
