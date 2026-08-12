from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from config import AGENT_REPORT_RECIPIENTS, OVERALL_REPORT_RECIPIENTS

# Safe fallbacks so GitHub Actions does not fail if these optional values are
# missing from config.py. This also lets you keep your current Allocation value.
try:
    from config import AUTOMATION_MAX_WORKERS
except ImportError:
    AUTOMATION_MAX_WORKERS = 8

try:
    from config import AUTOMATION_TIMEZONE
except ImportError:
    AUTOMATION_TIMEZONE = "Africa/Cairo"
from meta_api import MetaClient, fetch_full_snapshot
from reports import (
    build_agent_message_1,
    build_agent_message_2,
    build_overall_report,
    split_message,
)
from whatsapp import send_text_message


def env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


def env_bool(name: str, default: bool = False) -> bool:
    raw = env(name, "true" if default else "false").lower()
    return raw in {"1", "true", "yes", "y", "on"}


def mask_phone(value: str) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) <= 4:
        return digits or "-"
    return "*" * (len(digits) - 4) + digits[-4:]


def validate_required_config(*, require_whatsapp: bool = True) -> dict[str, str]:
    config = {
        "meta_access_token": env("META_ACCESS_TOKEN"),
        "meta_api_version": env("META_API_VERSION", "v26.0"),
        "whatsapp_access_token": env("WHATSAPP_ACCESS_TOKEN"),
        "whatsapp_phone_number_id": env("WHATSAPP_PHONE_NUMBER_ID"),
        "whatsapp_api_version": env("WHATSAPP_API_VERSION", "v26.0"),
    }
    required = ["meta_access_token"]
    if require_whatsapp:
        required.extend(["whatsapp_access_token", "whatsapp_phone_number_id"])
    missing = [key for key in required if not config[key]]
    if missing:
        raise RuntimeError(
            "Missing required environment secret(s): " + ", ".join(missing)
        )
    return config


def send_message_chunks(*, config: dict[str, str], recipient: str, body: str, label: str) -> int:
    sent = 0
    for chunk_no, chunk in enumerate(split_message(body), start=1):
        send_text_message(
            access_token=config["whatsapp_access_token"],
            phone_number_id=config["whatsapp_phone_number_id"],
            recipient=recipient,
            body=chunk,
            api_version=config["whatsapp_api_version"],
        )
        sent += 1
        print(f"SENT {label} -> {mask_phone(recipient)} | chunk {chunk_no}")
    return sent


def print_snapshot_summary(snapshot_df: pd.DataFrame, report_date: str) -> None:
    if snapshot_df.empty:
        print(f"Snapshot date: {report_date} | no rows")
        return

    spend = pd.to_numeric(snapshot_df.get("spend_today"), errors="coerce").fillna(0).sum()
    daily = pd.to_numeric(snapshot_df.get("active_daily_budget"), errors="coerce").fillna(0).sum()
    errors = int(snapshot_df.get("error", pd.Series(dtype=object)).notna().sum())
    active_accounts = int(
        (pd.to_numeric(snapshot_df.get("active_daily_budget"), errors="coerce").fillna(0) > 0).sum()
    )
    print(
        f"Snapshot date: {report_date} | accounts={len(snapshot_df)} | "
        f"active_budget_accounts={active_accounts} | spend={spend:,.2f} | "
        f"active_daily_budget={daily:,.2f} | account_errors={errors}"
    )


def main() -> int:
    dry_run = env_bool("DRY_RUN", False)
    config = validate_required_config(require_whatsapp=not dry_run)

    now_cairo = datetime.now(ZoneInfo(AUTOMATION_TIMEZONE))
    spend_date = now_cairo.date()
    print(
        f"Starting Meta budget automation at {now_cairo.isoformat()} "
        f"| spend_date={spend_date.isoformat()} | dry_run={dry_run}"
    )

    client = MetaClient(
        access_token=config["meta_access_token"],
        api_version=config["meta_api_version"],
    )

    accounts = client.get_ad_accounts()
    if accounts.empty:
        raise RuntimeError("Meta returned no eligible ad accounts. Reports were NOT sent.")

    snapshot_df, _details = fetch_full_snapshot(
        client,
        accounts,
        max_workers=AUTOMATION_MAX_WORKERS,
        spend_date=spend_date,
    )
    if snapshot_df.empty:
        raise RuntimeError("Snapshot is empty. Reports were NOT sent.")

    print_snapshot_summary(snapshot_df, spend_date.isoformat())

    if getattr(client, "discovery_errors", None):
        print("WARNING: ad-account discovery had source errors:")
        for item in client.discovery_errors:
            print(f"  - {item}")

    print("--- ACCOUNT FETCH DIAGNOSTICS ---")
    for _, row in snapshot_df.iterrows():
        print(
            f"{row.get('account_id', '-')} | {row.get('account_name', '-')} | "
            f"spend={float(row.get('spend_today') or 0):,.2f} ({row.get('spend_source', '-')}) | "
            f"campaign_budget={float(row.get('campaign_daily_budget') or 0):,.2f} | "
            f"adset_budget={float(row.get('adset_daily_budget') or 0):,.2f} | "
            f"total_budget={float(row.get('active_daily_budget') or 0):,.2f} | "
            f"campaign_spend_source={row.get('campaign_spend_source', '-')} rows={row.get('campaign_spend_rows', 0)} | "
            f"adset_spend_source={row.get('adset_spend_source', '-')} rows={row.get('adset_spend_rows', 0)} | "
            f"timezone={row.get('timezone_name', '-') or '-'} | "
            f"active_campaigns={row.get('active_campaigns_checked', 0)} "
            f"campaigns_spent={row.get('campaigns_with_spend', 0)} | "
            f"active_adsets={row.get('active_adsets_checked', 0)} "
            f"adsets_spent={row.get('adsets_with_spend', 0)} | "
            f"fetch_status={row.get('fetch_status', '-')} | errors={row.get('error_count', 0)} "
            f"warnings={row.get('warning_count', 0)}"
        )
        if row.get('error'):
            print(f"    ERROR DETAILS: {row.get('error')}")

    if _details:
        error_details = [item for item in _details if item.get("row_type") in {"ERROR", "WARNING"}]
        if error_details:
            print("--- META DETAIL ERRORS / WARNINGS ---")
            for item in error_details:
                print(
                    f"{item.get('row_type')} | account={item.get('account_id', '-')} | "
                    f"type={item.get('entity_type', '-')} | entity={item.get('entity_id', '-')} | "
                    f"name={item.get('entity_name', '-')} | {item.get('error', '-')}"
                )

    account_errors = snapshot_df[
        snapshot_df.get("error", pd.Series(index=snapshot_df.index, dtype=object)).notna()
    ]
    if not account_errors.empty:
        print("WARNING: some ad accounts failed during refresh:")
        for _, row in account_errors.iterrows():
            print(
                f"  - {row.get('account_id', '-')} | {row.get('account_name', '-')} | "
                f"{str(row.get('error', ''))[:500]}"
            )

    # Build all reports before sending anything. If report generation fails,
    # the workflow exits without partially sending a report batch.
    agent_reports: list[tuple[str, str, str, str]] = []
    for code, recipient in AGENT_REPORT_RECIPIENTS.items():
        agent_reports.append(
            (
                code,
                recipient,
                build_agent_message_1(snapshot_df, code),
                build_agent_message_2(snapshot_df, code),
            )
        )
    overall_report = build_overall_report(snapshot_df)

    if dry_run:
        print("DRY RUN enabled: no WhatsApp messages will be sent.")
        print("--- OVERALL REPORT PREVIEW ---")
        print(overall_report)
        for code, _recipient, message_1, message_2 in agent_reports:
            print(f"--- {code} MESSAGE 1 PREVIEW ---")
            print(message_1)
            print(f"--- {code} MESSAGE 2 PREVIEW ---")
            print(message_2)
        return 0

    sent_count = 0
    for code, recipient, message_1, message_2 in agent_reports:
        sent_count += send_message_chunks(
            config=config,
            recipient=recipient,
            body=message_1,
            label=f"{code} message 1",
        )
        sent_count += send_message_chunks(
            config=config,
            recipient=recipient,
            body=message_2,
            label=f"{code} message 2",
        )

    for recipient in OVERALL_REPORT_RECIPIENTS:
        sent_count += send_message_chunks(
            config=config,
            recipient=recipient,
            body=overall_report,
            label="OVERALL",
        )

    print(f"Automation completed successfully. WhatsApp chunks sent: {sent_count}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"AUTOMATION FAILED: {exc}", file=sys.stderr)
        raise
