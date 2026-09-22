from __future__ import annotations

import os
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests

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




def safe_num(value, default: float = 0.0) -> float:
    try:
        number = float(value)
        if pd.isna(number) or not __import__("math").isfinite(number):
            return default
        return number
    except Exception:
        return default


def mask_phone(value: str) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) <= 4:
        return digits or "-"
    return "*" * (len(digits) - 4) + digits[-4:]


def validate_required_config(*, require_whatsapp: bool = True) -> dict[str, str]:
    config = {
        "meta_access_token": env("META_ACCESS_TOKEN"),
        "meta_access_token_2": env("META_ACCESS_TOKEN_2"),
        "business_ids": env("TAHER_BUSINESS_IDS", env("BUSINESS_IDS")),
        "meta_api_version": env("META_API_VERSION", "v26.0"),
        "whatsapp_access_token": env("WHATSAPP_ACCESS_TOKEN"),
        "whatsapp_phone_number_id": env("WHATSAPP_PHONE_NUMBER_ID"),
        "whatsapp_api_version": env("WHATSAPP_API_VERSION", "v26.0"),
    }
    required = []
    if not (config["meta_access_token"] or config["meta_access_token_2"]):
        raise RuntimeError("Missing Meta access token: configure META_ACCESS_TOKEN and/or META_ACCESS_TOKEN_2")
    if require_whatsapp:
        required.extend(["whatsapp_access_token", "whatsapp_phone_number_id"])
    missing = [key for key in required if not config[key]]
    if missing:
        raise RuntimeError(
            "Missing required environment secret(s): " + ", ".join(missing)
        )
    return config



# Business IDs for Taher. The GitHub Variable overrides this default.
# No tokens are ever saved to a dataframe or written to the report.
DEFAULT_TAHER_BUSINESS_IDS = (
    "751488620224306",
    "1178859133269743",
    "1370772291128896",
)
ACCOUNT_NAME_PREFIXES = ("OK-FB-HR-", "OK-FB-NF-", "US-FB-HR-", "US-BO-HR-")
META_ACCOUNT_FIELDS = (
    "id,account_id,name,account_status,currency,balance,amount_spent,"
    "spend_cap,funding_source_details,timezone_name,timezone_offset_hours_utc"
)


def configured_business_ids(raw: str) -> list[str]:
    values = [x.strip() for x in re.split(r"[,;\n]+", raw or "") if x.strip()]
    if not values:
        values = list(DEFAULT_TAHER_BUSINESS_IDS)
    invalid = [value for value in values if not re.fullmatch(r"\d+", value)]
    if invalid:
        raise RuntimeError("TAHER_BUSINESS_IDS must contain numeric IDs separated by commas")
    return list(dict.fromkeys(values))


def redact_meta_error(exc: Exception, tokens: dict[str, str]) -> str:
    message = str(exc)
    for token in tokens.values():
        if token:
            message = message.replace(token, "[REDACTED]")
    return message[:600]


def graph_accounts(token: str, api_version: str, path: str, fields: str) -> list[dict]:
    """Read every pagination page without printing a URL containing access_token."""
    url = f"https://graph.facebook.com/{api_version}/{path}"
    params = {"access_token": token, "fields": fields, "limit": 500}
    rows: list[dict] = []
    while url:
        response = requests.get(url, params=params, timeout=60)
        if not response.ok:
            try:
                error = response.json().get("error", {})
                message = error.get("message", "Meta account discovery error")
                code = error.get("code", response.status_code)
            except (ValueError, AttributeError):
                message, code = "Meta account discovery error", response.status_code
            raise RuntimeError(f"Meta API code={code}: {message[:300]}")
        data = response.json()
        rows.extend(data.get("data", []))
        next_url = data.get("paging", {}).get("next")
        if next_url and not next_url.startswith("https://graph.facebook.com/"):
            raise RuntimeError("Unexpected pagination host returned by Meta")
        url, params = next_url, None
    return rows


def account_key(row: dict) -> str:
    value = str(row.get("id") or row.get("account_id") or "").strip()
    return value.replace("act_", "")


def suitable_baseline_account(row: dict, business_ids: list[str]) -> bool:
    source = str(row.get("source") or "")
    # Preserve existing /me discovery rules, but don't add unrelated businesses
    # that a repository's older static config.py may include.
    if source.startswith("business/"):
        return any(f"business/{business_id}/" in source for business_id in business_ids)
    name = str(row.get("name") or "").strip().upper()
    return any(prefix in name for prefix in ACCOUNT_NAME_PREFIXES)


def discover_all_accounts(clients: dict[str, MetaClient], business_ids: list[str],
                          api_version: str) -> tuple[dict[str, list[tuple[str, dict]]], list[str]]:
    """Return one account ID with its available token candidates (never token values)."""
    discovered: dict[str, dict[str, dict]] = {}
    warnings: list[str] = []
    business_counts: Counter = Counter()
    business_edge_success: Counter = Counter()
    all_tokens = {label: client.access_token for label, client in clients.items()}

    def add(row: dict, token_label: str, source: str) -> None:
        value = dict(row)
        key = account_key(value)
        if not key:
            return
        value["id"] = f"act_{key}"
        value["account_id"] = key
        value["source"] = source
        per_token = discovered.setdefault(key, {})
        if token_label in per_token:
            # Keep fields already provided by meta_api.get_ad_accounts(); enrich
            # only if the explicit Business API supplies a real value.
            old = per_token[token_label]
            old.update({k: v for k, v in value.items() if v is not None and v != ""})
        else:
            per_token[token_label] = value

    for label, client in clients.items():
        # Keep the old MetaClient's discovery and its special account metadata.
        try:
            baseline = client.get_ad_accounts()
            if not baseline.empty:
                for row in baseline.to_dict("records"):
                    if suitable_baseline_account(row, business_ids):
                        add(row, label, str(row.get("source") or "me/adaccounts"))
            for issue in getattr(client, "discovery_errors", []) or []:
                warnings.append(f"{label}: {redact_meta_error(Exception(issue), all_tokens)}")
        except Exception as exc:
            warnings.append(f"{label}: built-in discovery failed: {redact_meta_error(exc, all_tokens)}")

        # Explicitly query ALL selected businesses with EACH available token.
        # This ensures a new business is included even if config.py is unchanged.
        for business_id in business_ids:
            found_here = set()
            for edge in ("owned_ad_accounts", "client_ad_accounts"):
                source = f"business/{business_id}/{edge}"
                try:
                    try:
                        rows = graph_accounts(client.access_token, api_version,
                                              f"{business_id}/{edge}", META_ACCOUNT_FIELDS)
                    except RuntimeError:
                        # A restricted optional account field should not hide
                        # otherwise accessible accounts.
                        rows = graph_accounts(client.access_token, api_version,
                                              f"{business_id}/{edge}",
                                              "id,account_id,name,account_status,currency")
                    business_edge_success[business_id] += 1
                    for row in rows:
                        add(row, label, source)
                        if account_key(row):
                            found_here.add(account_key(row))
                except Exception as exc:
                    warnings.append(f"{label} | {source}: {redact_meta_error(exc, all_tokens)}")
            business_counts[(label, business_id)] += len(found_here)

        # Preserve the existing name-coded accounts that the token can access
        # outside the configured businesses. Do not include unrelated names.
        try:
            rows = graph_accounts(client.access_token, api_version, "me/adaccounts",
                                  "id,account_id,name,account_status,currency")
            for row in rows:
                name = str(row.get("name") or "").upper()
                if any(prefix in name for prefix in ACCOUNT_NAME_PREFIXES):
                    add(row, label, "me/adaccounts")
        except Exception as exc:
            warnings.append(f"{label} | me/adaccounts: {redact_meta_error(exc, all_tokens)}")

    unverified = [bid for bid in business_ids if not business_edge_success[bid]]
    if unverified:
        raise RuntimeError(
            "Neither token could access the owned/client edges of Business ID(s): "
            + ", ".join(unverified)
            + ". Reports were NOT sent to avoid an incomplete total."
        )

    for business_id in business_ids:
        summary = " | ".join(f"{label}={business_counts[(label, business_id)]}"
                             for label in clients)
        print(f"Business {business_id} discovered: {summary}")
    print(f"Unique ad accounts: {len(discovered)}; configured tokens: {len(clients)}")
    # Prefer token_1 on overlaps; fallback to token_2 if fetching fails.
    return {key: [(label, rows[label]) for label in clients if label in rows]
            for key, rows in discovered.items()}, warnings


def snapshot_failed(frame: pd.DataFrame, expected_account_id: str) -> bool:
    if frame.empty:
        return True
    row = frame.iloc[0]
    actual = str(row.get("account_id") or "").replace("act_", "")
    if actual and actual != expected_account_id:
        return True
    return str(row.get("fetch_status") or "").upper() == "ERROR"


def fetch_account_with_fallback(account_id: str, candidates: list[tuple[str, dict]],
                                clients: dict[str, MetaClient], spend_date) -> tuple[pd.DataFrame, list[dict], str, list[str]]:
    errors: list[str] = []
    first_partial = None
    all_tokens = {label: client.access_token for label, client in clients.items()}
    for label, row in candidates:
        try:
            frame, details = fetch_full_snapshot(
                clients[label], pd.DataFrame([row]), max_workers=1, spend_date=spend_date
            )
            if not snapshot_failed(frame, account_id):
                row_error = frame.iloc[0].get("error")
                if row_error is None or pd.isna(row_error) or str(row_error).strip() == "":
                    return frame, details, label, errors
                if first_partial is None:
                    first_partial = (frame, details, label)
                errors.append(f"{label}: partial snapshot: {redact_meta_error(Exception(row_error), all_tokens)}")
                continue
            errors.append(f"{label}: account fetch_status=ERROR or no matching row")
        except Exception as exc:
            errors.append(f"{label}: {redact_meta_error(exc, all_tokens)}")
    if first_partial is not None:
        frame, details, label = first_partial
        return frame, details, label, errors
    raise RuntimeError(f"Account act_{account_id} failed for all available tokens: {'; '.join(errors)}")


def collect_multi_token_snapshot(clients: dict[str, MetaClient], business_ids: list[str],
                                 api_version: str, spend_date,
                                 max_workers: int) -> tuple[pd.DataFrame, list[dict], list[str]]:
    discovered, warnings = discover_all_accounts(clients, business_ids, api_version)
    if not discovered:
        raise RuntimeError("No eligible ad accounts discovered from either token. Reports were NOT sent.")
    frames: list[pd.DataFrame] = []
    details: list[dict] = []
    failed: list[str] = []
    token_counts: Counter = Counter()
    with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), 10))) as executor:
        futures = {executor.submit(fetch_account_with_fallback, account_id, candidates,
                                   clients, spend_date): account_id
                   for account_id, candidates in discovered.items()}
        for future in as_completed(futures):
            account_id = futures[future]
            try:
                frame, result_details, label, issues = future.result()
                frames.append(frame)
                details.extend(result_details)
                token_counts[label] += 1
                print(f"Fetched act_{account_id} via {label}")
                for item in issues:
                    warnings.append(f"act_{account_id}: {item}")
            except Exception as exc:
                failed.append(redact_meta_error(exc, {k: c.access_token for k, c in clients.items()}))
    if failed:
        # Never send an incomplete budget/coverage total as an overall total.
        raise RuntimeError(f"{len(failed)} accounts could not be fetched. Reports NOT sent. "
                           + " | ".join(failed[:12]))
    print("Snapshot by token: " + ", ".join(f"{label}={token_counts[label]}" for label in clients))
    return pd.concat(frames, ignore_index=True), details, warnings


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

    clients = {
        label: MetaClient(access_token=token, api_version=config["meta_api_version"])
        for label, token in (
            ("token_1", config["meta_access_token"]),
            ("token_2", config["meta_access_token_2"]),
        )
        if token
    }
    business_ids = configured_business_ids(config["business_ids"])
    print("Configured Business IDs: " + ", ".join(business_ids))
    snapshot_df, _details, discovery_warnings = collect_multi_token_snapshot(
        clients, business_ids, config["meta_api_version"], spend_date,
        max_workers=AUTOMATION_MAX_WORKERS,
    )
    if snapshot_df.empty:
        raise RuntimeError("Snapshot is empty. Reports were NOT sent.")

    print_snapshot_summary(snapshot_df, spend_date.isoformat())

    if discovery_warnings:
        print("WARNING: account discovery/fallback had source errors:")
        for item in discovery_warnings:
            print(f"  - {item}")

    print("--- ACCOUNT FETCH DIAGNOSTICS ---")
    for _, row in snapshot_df.iterrows():
        print(
            f"{row.get('account_id', '-')} | {row.get('account_name', '-')} | "
            f"spend={safe_num(row.get('spend_today')):,.2f} ({row.get('spend_source', '-')}) | "
            f"campaign_budget={safe_num(row.get('campaign_daily_budget')):,.2f} | "
            f"adset_budget={safe_num(row.get('adset_daily_budget')):,.2f} | "
            f"total_budget={safe_num(row.get('active_daily_budget')):,.2f} | "
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
        budget_details = [item for item in _details if item.get("row_type") == "budget"]
        if budget_details:
            print("--- BUDGET ITEMS COUNTED ---")
            for item in sorted(budget_details, key=lambda x: (str(x.get("account_name", "")), str(x.get("budget_level", "")), str(x.get("campaign_name") or x.get("adset_name") or ""))):
                item_name = item.get("campaign_name") or item.get("adset_name") or item.get("entity_name") or "-"
                item_id = item.get("adset_id") or item.get("campaign_id") or item.get("entity_id") or "-"
                print(
                    f"BUDGET | account={item.get('account_id', '-')} | {item.get('account_name', '-')} | "
                    f"level={item.get('budget_level', '-')} | id={item_id} | name={item_name} | "
                    f"spend_today={safe_num(item.get('spend_today')):,.2f} | daily_budget={safe_num(item.get('daily_budget')):,.2f}"
                )

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
