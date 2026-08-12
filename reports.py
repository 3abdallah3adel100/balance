from __future__ import annotations

import math
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


def pct(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "N/A"
    return f"{float(value):,.2f}%"


def days(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "N/A"
    return f"{float(value):,.2f} days"


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
            f"Active Daily Budget is aligned with Allocation Budget "
            f"({diff:+,.2f} EGP / {diff_pct:+.1f}%)."
        )
    if diff < 0 and abs_pct >= ALLOCATION_SIGNIFICANT_DIFF_PCT:
        return (
            f"Active Daily Budget is significantly BELOW Allocation by "
            f"{abs(diff):,.2f} EGP ({abs_pct:.1f}%)."
        )
    if diff < 0:
        return (
            f"Active Daily Budget is below Allocation by "
            f"{abs(diff):,.2f} EGP ({abs_pct:.1f}%)."
        )
    if abs_pct >= ALLOCATION_SIGNIFICANT_DIFF_PCT:
        return (
            f"Active Daily Budget is significantly ABOVE Allocation by "
            f"{abs(diff):,.2f} EGP ({abs_pct:.1f}%)."
        )
    return (
        f"Active Daily Budget is above Allocation by "
        f"{abs(diff):,.2f} EGP ({abs_pct:.1f}%)."
    )


def _active_agent_accounts(snapshot_df: pd.DataFrame, code: str) -> pd.DataFrame:
    if snapshot_df.empty:
        return pd.DataFrame()
    df = snapshot_df[
        (snapshot_df["buyer_code"] == code)
        & (pd.to_numeric(snapshot_df["active_daily_budget"], errors="coerce").fillna(0) > 0)
    ].copy()
    return df.sort_values("account_name")


def build_agent_message_1(snapshot_df: pd.DataFrame, code: str) -> str:
    name = MEDIA_BUYER_MAP.get(code, code)
    df = _active_agent_accounts(snapshot_df, code)
    lines = [f"📊 *{name} ({code}) — Balance & Daily Budget*", ""]

    if df.empty:
        lines.append("No ad accounts with active daily-budget campaigns were found.")
        lines.extend(["", f"Overall Active Daily Budget for {name} ({code}): 0.00 EGP", "Overall Balance: 0.00 EGP"])
        return "\n".join(lines)

    for _, row in df.iterrows():
        coverage = row.get("coverage_days")
        daily_budget = float(row.get("active_daily_budget") or 0)
        balance = row.get("balance")

        if coverage is not None and pd.notna(coverage) and float(coverage) <= CRITICAL_COVERAGE_DAYS:
            alarm = "🚨 CRITICAL — Recharge needed. Balance covers 24 hours or less."
        elif coverage is not None and pd.notna(coverage) and float(coverage) < TARGET_COVERAGE_DAYS:
            alarm = "⚠️ Balance is below the 3-day target."
        elif coverage is None or pd.isna(coverage):
            alarm = "⚠️ Balance unavailable — verify balance source."
        else:
            alarm = "✅ Balance coverage is healthy."

        lines.extend([
            f"*Ad Account ID:* {row.get('account_id', '-')}",
            f"*Ad Account Name:* {row.get('account_name', '-')}",
            f"*Active Daily Budget:* {money(daily_budget, row.get('currency', 'EGP'))}",
            f"*Balance:* {money(balance, row.get('currency', 'EGP'))}",
            f"*Balance Coverage:* {days(coverage)}",
            f"*Alarm:* {alarm}",
            "",
        ])

    overall_budget = pd.to_numeric(df["active_daily_budget"], errors="coerce").fillna(0).sum()
    overall_balance = pd.to_numeric(df["balance"], errors="coerce").fillna(0).sum()
    lines.extend([
        "──────────────",
        f"*Overall Active Daily Budget for {name} ({code}):* {money(overall_budget)}",
        f"*Overall Balance:* {money(overall_balance)}",
    ])
    return "\n".join(lines).strip()


def build_agent_message_2(snapshot_df: pd.DataFrame, code: str) -> str:
    name = MEDIA_BUYER_MAP.get(code, code)
    df = _active_agent_accounts(snapshot_df, code)
    if not df.empty:
        coverage = pd.to_numeric(df["coverage_days"], errors="coerce")
        df = df[coverage <= CRITICAL_COVERAGE_DAYS].copy()

    lines = [f"🚨 *{name} Team ({code}) — Recharge Required*", ""]
    if df.empty:
        lines.append("No critical accounts currently need a recharge alert.")
        return "\n".join(lines)

    total_required = 0.0
    for _, row in df.iterrows():
        required = float(row.get("required_for_3_days") or 0)
        total_required += required
        lines.extend([
            f"*Acc ID:* {row.get('account_id', '-')}",
            f"*Amount to reach 3-day coverage:* {money(required, row.get('currency', 'EGP'))}",
            "",
        ])
    lines.extend(["──────────────", f"*Total Recharge Required:* {money(total_required)}"])
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
        f"*Active Daily Budget:* {money(total_daily)}",
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
            f"Active Daily Budget: {money(daily)}",
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
