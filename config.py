import re

# ============================================================
# EASY-TO-EDIT SETTINGS
# ============================================================

BUSINESS_IDS = [
    "751488620224306",
    "1178859133269743",
]

MEDIA_BUYER_MAP = {
    "AA": "Abdallah Adel",
    "HM": "Ahmed Hesham",
    "BM": "Bassem Shalawy",
    "EK": "Esraa Kamal",
    "MA": "Mahmoud",
    "AF": "Amr Fathy",
    "SQ": "(R)Ahmed Sharkawy",
    "OS": "(R)Osama Serwe",
    "MM": "(R)Mohamed Mahmoud",
    "NB": "(R)Mohamed Nabih",
}

# Personal report routing: each code receives its own 2-message report.
AGENT_REPORT_RECIPIENTS = {
    "AA": "201280871971",
    "HM": "201015177863",
    "BM": "201111901470",
}

# Overall management report goes ONLY to these numbers.
OVERALL_REPORT_RECIPIENTS = [
    "201280871971",
    "201098320008",
]

# >>> EDIT THIS VALUE MANUALLY WHEN YOUR OVERALL ALLOCATION CHANGES <<<
# This value is used ONLY in the Overall Management Report.
# It does NOT affect any agent balance/recharge calculation.
OVERALL_ALLOCATION_BUDGET = 80000.0  # EGP per day, example: 250000.0

# Balance alert/recharge rules for personal agent reports.
CRITICAL_COVERAGE_DAYS = 1.0
TARGET_COVERAGE_DAYS = 3.0

# Note thresholds comparing Active Daily Budget vs Overall Allocation Budget.
# Difference within +/- 10% => aligned.
ALLOCATION_ALIGNED_TOLERANCE_PCT = 10.0
# Difference >= 30% => significantly below/above.
ALLOCATION_SIGNIFICANT_DIFF_PCT = 30.0

# Meta Graph money fields such as daily_budget/balance are normally returned
# in the account currency's smallest unit. For EGP we convert /100.
CURRENCY_MINOR_UNIT_SCALE = {
    "EGP": 100.0,
    "USD": 100.0,
    "EUR": 100.0,
    "GBP": 100.0,
    "AED": 100.0,
    "SAR": 100.0,
}
DEFAULT_MINOR_UNIT_SCALE = 100.0

# Keep the same account-scope behavior as the old dashboard:
# - all accounts discovered inside BUSINESS_IDS are eligible
# - /me/adaccounts accounts are included only if their names match these prefixes
INCLUDE_ME_AD_ACCOUNTS = True
ME_ACCOUNT_NAME_PREFIXES = (
    "OK-FB-HR-",
    "OK-FB-NF-",
    "US-FB-HR-",
    "US-BO-HR-",
)

# If Meta's balance field does not represent the usable prepaid funds for a
# specific account, you can override the available balance here in EGP.
# Key can be either "123456789" or "act_123456789".
MANUAL_BALANCE_OVERRIDES = {
    # "123456789": 50000.0,
}


def normalize_text(value) -> str:
    return str(value or "").strip().upper()


def clean_account_id(value) -> str:
    return str(value or "").replace("act_", "").strip()


def extract_buyer_code(account_name: str) -> str:
    text = normalize_text(account_name)
    for code in MEDIA_BUYER_MAP:
        pattern = rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])"
        if re.search(pattern, text):
            return code
    return "UNKNOWN"


def buyer_name(code: str) -> str:
    return MEDIA_BUYER_MAP.get(code, "Unknown")
