from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable

import pandas as pd

from config import (
    ALLOCATION_ALIGNED_TOLERANCE_PCT,
    ALLOCATION_SIGNIFICANT_DIFF_PCT,
    CRITICAL_COVERAGE_DAYS,
    MEDIA_BUYER_MAP,
    OVERALL_ALLOCATION_BUDGET,
    TARGET_COVERAGE_DAYS,
)


def money(value, currency="EGP") -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "N/A"
    return f"{float(value):,.2f} {currency}"


def whole_money(value, currency="EGP") -> str:
    """Round message-2 amount to the nearest 100; 50 or more rounds up."""
    if value is None:
        return "N/A"
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return "N/A"
        rounded = (number / Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * Decimal("100")
        return f"{int(rounded):,} {currency}"
    except (InvalidOperation, ValueError, TypeError):
        return "N/A"


def pct(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "N/A"
    return f"{float(value):,.2f}%"


def days(value) -> str:
    """Format coverage as whole days + hours, e.g. 1 day . 12 hours."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "N/A"

    total_hours = max(0, int(round(float(value) * 24)))
    whole_days, hours = divmod(total_hours, 24)
    day_label = "day" if whole_days == 1 else "days"
    hour_label = "hour" if hours == 1 else "hours"
    return f"{whole_days} {day_label} . {hours} {hour_label}"


def safe_ratio_pct(a: float, b: float) -> float | None:
    if b and b > 0:
        return (a / b) * 100.0
    return None


def build_allocation_note(active_daily_budget: float, allocation_budget: float) -> str:
    if allocation_budget <= 0:
        return "Allocation Budget is not configured yet. Edit OVERALL_ALLOCATION_BUDGET in config.py."

    diff = active_daily_budget - allocation_budget
    diff_pct = (diff / allocation_budget) * 100.0
    abs_pct = abs(diff_pct)

    if abs_pct <= ALLOCATION_ALIGNED_TOLERANCE_PCT:
        return (
            f"Daily Budget (spending campaigns) is aligned with Allocation Budget "
            f"({diff:+,.2f} EGP / {diff_pct:+.1f}%)."
        )
    if diff < 0 and abs_pct >= ALLOCATION_SIGNIFICANT_DIFF_PCT:
        return (
            f"Daily Budget (spending campaigns) is significantly BELOW Allocation by "
            f"{abs(diff):,.2f} EGP ({abs_pct:.1f}%)."
        )
    if diff < 0:
        return (
            f"Daily Budget (spending campaigns) is below Allocation by "
            f"{abs(diff):,.2f} EGP ({abs_pct:.1f}%)."
        )
    if abs_pct >= ALLOCATION_SIGNIFICANT_DIFF_PCT:
        return (
            f"Daily Budget (spending campaigns) is significantly ABOVE Allocation by "
            f"{abs(diff):,.2f} EGP ({abs_pct:.1f}%)."
        )
    return (
        f"Daily Budget (spending campaigns) is above Allocation by "
        f"{abs(diff):,.2f} EGP ({abs_pct:.1f}%)."
    )


def _spending_agent_accounts(snapshot_df: pd.DataFrame, code: str) -> pd.DataFrame:
    if snapshot_df.empty:
        return pd.DataFrame()
    df = snapshot_df[
        (snapshot_df["buyer_code"] == code)
        & (pd.to_numeric(snapshot_df["active_daily_budget"], errors="coerce").fillna(0) > 0)
    ].copy()
    return df.sort_values("account_name")


def build_agent_message_1(snapshot_df: pd.DataFrame, code: str) -> str:
    name = MEDIA_BUYER_MAP.get(code, code)
    df = _spending_agent_accounts(snapshot_df, code)
    lines = [f"📊 *{name} ({code}) — Balance & Daily Budget*", ""]

    if df.empty:
        lines.append("No ad accounts with Spend Today > 0 and a daily budget were found.")
        lines.extend(["", f"Overall Daily Budget for {name} ({code}): 0.00 EGP", "Overall Balance: 0.00 EGP"])
        return "\n".join(lines)

    for _, row in df.iterrows():
        coverage = row.get("coverage_days")
        daily_budget = float(row.get("active_daily_budget") or 0)
        balance = row.get("balance")

        if coverage is not None and pd.notna(coverage) and float(coverage) <= CRITICAL_COVERAGE_DAYS:
            alarm = "⚠️ Balance is below the 3-day target."
        elif coverage is None or pd.isna(coverage):
            alarm = "⚠️ Balance unavailable — verify balance source."
        else:
            alarm = "✅ Balance coverage is above the 1-day alarm threshold."

        lines.extend([
            f"*Ad Account ID:* {row.get('account_id', '-')}",
            f"*Ad Account Name:* {row.get('account_name', '-')}",
            f"*Daily Budget:* {money(daily_budget, row.get('currency', 'EGP'))}",
            f"*Balance:* {money(balance, row.get('currency', 'EGP'))}",
            f"*Balance Coverage:* {days(coverage)}",
            f"*Alarm:* {alarm}",
            "",
        ])

    overall_budget = pd.to_numeric(df["active_daily_budget"], errors="coerce").fillna(0).sum()
    overall_balance = pd.to_numeric(df["balance"], errors="coerce").fillna(0).sum()
    lines.extend([
        "──────────────",
        f"*Overall Daily Budget for {name} ({code}):* {money(overall_budget)}",
        f"*Overall Balance:* {money(overall_balance)}",
    ])
    return "\n".join(lines).strip()


def build_agent_message_2(snapshot_df: pd.DataFrame, code: str) -> str:
    """Taher Team recharge invoice for accounts below 3 days coverage.

    Only this message logic is changed:
    - show the account only when coverage is below TARGET_COVERAGE_DAYS
    - Balance line = amount needed to reach exactly 3 days
    """
    df = _spending_agent_accounts(snapshot_df, code)
    if not df.empty:
        coverage = pd.to_numeric(df["coverage_days"], errors="coerce")
        df = df[coverage < TARGET_COVERAGE_DAYS].copy()

    lines = ["*Taher Team*", ""]
    if df.empty:
        lines.append("No accounts currently need recharge to reach the 3-day target.")
        return "\n".join(lines).strip()

    for _, row in df.iterrows():
        daily_budget = pd.to_numeric(pd.Series([row.get("active_daily_budget")]), errors="coerce").iloc[0]
        current_balance = pd.to_numeric(pd.Series([row.get("balance")]), errors="coerce").iloc[0]

        if pd.isna(daily_budget) or pd.isna(current_balance) or float(daily_budget) <= 0:
            continue

        recharge_needed = max(
            0.0,
            (float(daily_budget) * float(TARGET_COVERAGE_DAYS)) - float(current_balance),
        )

        if recharge_needed <= 0:
            continue

        lines.extend([
            f"Acc ID : {row.get('account_id', '-')}",
            f"Balance : {whole_money(recharge_needed, row.get('currency', 'EGP'))}",
            "",
        ])
    return "\n".join(lines).strip()

def build_overall_report(snapshot_df: pd.DataFrame, allocation_budget: float | None = None) -> str:
    allocation = OVERALL_ALLOCATION_BUDGET if allocation_budget is None else float(allocation_budget)
    if snapshot_df.empty:
        total_spend = 0.0
        total_daily = 0.0
    else:
        total_spend = pd.to_numeric(snapshot_df["spend_today"], errors="coerce").fillna(0).sum()
        total_daily = pd.to_numeric(snapshot_df["active_daily_budget"], errors="coerce").fillna(0).sum()

    spend_vs_allocation = safe_ratio_pct(total_spend, allocation)
    remaining_allocation = allocation - total_spend if allocation > 0 else None

    lines = [
        "📊 *Overall Budget Performance*",
        "",
        f"*Spend Today:* {money(total_spend)}",
        f"*Daily Budget:* {money(total_daily)}",
        f"*Allocation Budget:* {money(allocation) if allocation > 0 else 'NOT CONFIGURED'}",
        f"*Spend vs Allocation:* {pct(spend_vs_allocation)}",
        f"*Remaining Allocation:* {money(remaining_allocation) if remaining_allocation is not None else 'N/A'}",
        f"*Note:* {build_allocation_note(total_daily, allocation)}",
        "",
        "──────────────",
        "👥 *Agents Performance*",
    ]

    if snapshot_df.empty:
        lines.append("No account data available.")
        return "\n".join(lines)

    ordered_codes = [c for c in MEDIA_BUYER_MAP if c in set(snapshot_df["buyer_code"].astype(str))]
    unknown_exists = (snapshot_df["buyer_code"].astype(str) == "UNKNOWN").any()
    if unknown_exists:
        ordered_codes.append("UNKNOWN")

    for code in ordered_codes:
        agent_df = snapshot_df[snapshot_df["buyer_code"] == code].copy()
        spend = pd.to_numeric(agent_df["spend_today"], errors="coerce").fillna(0).sum()
        daily = pd.to_numeric(agent_df["active_daily_budget"], errors="coerce").fillna(0).sum()
        spend_vs_daily = safe_ratio_pct(spend, daily)
        remaining_daily = daily - spend
        name = MEDIA_BUYER_MAP.get(code, "Unknown")
        lines.extend([
            "",
            f"*{name} ({code})*",
            f"Daily Budget: {money(daily)}",
            f"Spend Today: {money(spend)}",
            f"Spend vs Daily Budget: {pct(spend_vs_daily)}",
            f"Remaining vs Daily Budget: {money(remaining_daily)}",
        ])

    return "\n".join(lines).strip()


def split_message(message: str, max_chars: int = 3500) -> list[str]:
    if len(message) <= max_chars:
        return [message]
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in message.splitlines():
        added = len(line) + 1
        if current and length + added > max_chars:
            chunks.append("\n".join(current).strip())
            current = []
            length = 0
        current.append(line)
        length += added
    if current:
        chunks.append("\n".join(current).strip())
    return chunks
