from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from config import (
    AGENT_REPORT_RECIPIENTS,
    CRITICAL_COVERAGE_DAYS,
    OVERALL_ALLOCATION_BUDGET,
    OVERALL_REPORT_RECIPIENTS,
    TARGET_COVERAGE_DAYS,
)
from meta_api import MetaClient, fetch_full_snapshot
from reports import (
    build_agent_message_1,
    build_agent_message_2,
    build_overall_report,
    split_message,
)
from whatsapp import send_text_message

st.set_page_config(page_title="Meta Budget & Balance Monitor", layout="wide")


def secret(name: str, default=None):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def check_password() -> bool:
    app_password = secret("APP_PASSWORD", "")
    if not app_password:
        return True
    if st.session_state.get("password_ok"):
        return True
    st.title("🔐 Meta Budget & Balance Monitor")
    entered = st.text_input("Password", type="password")
    if st.button("Login", type="primary"):
        if entered == app_password:
            st.session_state["password_ok"] = True
            st.rerun()
        else:
            st.error("Wrong password")
    return False


if not check_password():
    st.stop()

st.title("💰 Meta Budget & Balance Monitor")
st.caption("Spending Campaign Budget • Today Spend • Balance Coverage • WhatsApp Recharge Alerts")

meta_token = secret("META_ACCESS_TOKEN", "")
meta_version = secret("META_API_VERSION", "v26.0")
wa_token = secret("WHATSAPP_ACCESS_TOKEN", "")
wa_phone_number_id = secret("WHATSAPP_PHONE_NUMBER_ID", "")
wa_version = secret("WHATSAPP_API_VERSION", "v26.0")

with st.sidebar:
    st.header("Settings")
    st.metric("Critical Coverage", f"≤ {CRITICAL_COVERAGE_DAYS:g} day")
    st.metric("Recharge Target", f"{TARGET_COVERAGE_DAYS:g} days")
    if OVERALL_ALLOCATION_BUDGET > 0:
        st.metric("Overall Allocation", f"{OVERALL_ALLOCATION_BUDGET:,.2f} EGP")
    else:
        st.warning("OVERALL_ALLOCATION_BUDGET = 0. Edit config.py before using the overall allocation comparison.")
    max_workers = st.slider("Parallel Meta requests", 1, 16, 8)

if not meta_token:
    st.error("Missing META_ACCESS_TOKEN in Streamlit secrets.")
    st.stop()

if "snapshot_df" not in st.session_state:
    st.session_state["snapshot_df"] = pd.DataFrame()
    st.session_state["budget_details"] = []
    st.session_state["last_refresh"] = None

col1, col2 = st.columns([1, 1])
with col1:
    refresh = st.button("🔄 Refresh Meta Data", type="primary", use_container_width=True)
with col2:
    send = st.button("📲 Send WhatsApp Reports", use_container_width=True, disabled=st.session_state["snapshot_df"].empty)

if refresh:
    client = MetaClient(access_token=meta_token, api_version=meta_version)
    with st.status("Fetching ad accounts, today spend, spending budgets and balances...", expanded=True) as status:
        try:
            accounts = client.get_ad_accounts()
            st.write(f"Ad accounts discovered: {len(accounts)}")
            if accounts.empty:
                status.update(label="No ad accounts found", state="error")
                st.stop()
            snapshot_df, details = fetch_full_snapshot(client, accounts, max_workers=max_workers)
            st.session_state["snapshot_df"] = snapshot_df
            st.session_state["budget_details"] = details
            st.session_state["last_refresh"] = datetime.now(ZoneInfo("Africa/Cairo"))
            error_count = int(snapshot_df["error"].notna().sum()) if "error" in snapshot_df else 0
            status.update(label=f"Refresh complete — {len(snapshot_df)} accounts, {error_count} errors", state="complete")
        except Exception as exc:
            status.update(label="Refresh failed", state="error")
            st.exception(exc)

snapshot_df = st.session_state["snapshot_df"]
if not snapshot_df.empty:
    total_spend = pd.to_numeric(snapshot_df["spend_today"], errors="coerce").fillna(0).sum()
    total_daily = pd.to_numeric(snapshot_df["active_daily_budget"], errors="coerce").fillna(0).sum()
    active_accounts = int((pd.to_numeric(snapshot_df["active_daily_budget"], errors="coerce").fillna(0) > 0).sum())
    critical_accounts = int((pd.to_numeric(snapshot_df["coverage_days"], errors="coerce") <= CRITICAL_COVERAGE_DAYS).fillna(False).sum())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Spend Today", f"{total_spend:,.2f} EGP")
    c2.metric("Daily Budget (Spending)", f"{total_daily:,.2f} EGP")
    c3.metric("Accounts with Spending Budget", f"{active_accounts}")
    c4.metric("Critical ≤ 1 Day", f"{critical_accounts}")

    if st.session_state["last_refresh"]:
        st.caption(f"Last refresh: {st.session_state['last_refresh'].strftime('%Y-%m-%d %H:%M:%S')} Cairo")

    display = snapshot_df.copy()
    for col in ["spend_today", "active_daily_budget", "balance", "coverage_days", "required_for_3_days"]:
        if col in display:
            display[col] = pd.to_numeric(display[col], errors="coerce").round(2)
    display["status"] = "Healthy"
    coverage = pd.to_numeric(display["coverage_days"], errors="coerce")
    display.loc[coverage < TARGET_COVERAGE_DAYS, "status"] = "Below 3-day target"
    display.loc[coverage <= CRITICAL_COVERAGE_DAYS, "status"] = "CRITICAL"
    display.loc[coverage.isna() & (pd.to_numeric(display["active_daily_budget"], errors="coerce").fillna(0) > 0), "status"] = "Balance unavailable"

    st.subheader("Ad Accounts")
    cols = [
        "buyer_code", "media_buyer", "account_id", "account_name", "currency",
        "spend_today", "active_daily_budget", "balance", "coverage_days",
        "required_for_3_days", "balance_source", "active_budget_items", "status", "error"
    ]
    st.dataframe(display[[c for c in cols if c in display.columns]], use_container_width=True, hide_index=True)

    with st.expander("Spending Budget Details (CBO / ABO)"):
        details = pd.DataFrame(st.session_state["budget_details"])
        if details.empty:
            st.info("No spending daily-budget items found.")
        else:
            st.dataframe(details, use_container_width=True, hide_index=True)

    with st.expander("Preview WhatsApp Reports"):
        tabs = st.tabs(["Overall"] + list(AGENT_REPORT_RECIPIENTS.keys()))
        with tabs[0]:
            st.code(build_overall_report(snapshot_df), language=None)
        for tab, code in zip(tabs[1:], AGENT_REPORT_RECIPIENTS.keys()):
            with tab:
                st.markdown("**Message 1**")
                st.code(build_agent_message_1(snapshot_df, code), language=None)
                st.markdown("**Message 2**")
                st.code(build_agent_message_2(snapshot_df, code), language=None)

if send:
    if not wa_token or not wa_phone_number_id:
        st.error("Missing WHATSAPP_ACCESS_TOKEN or WHATSAPP_PHONE_NUMBER_ID in Streamlit secrets.")
    else:
        results = []
        with st.status("Sending WhatsApp reports...", expanded=True) as status:
            # Personal 2-message report per configured agent.
            for code, recipient in AGENT_REPORT_RECIPIENTS.items():
                messages = [
                    build_agent_message_1(snapshot_df, code),
                    build_agent_message_2(snapshot_df, code),
                ]
                for message_no, message in enumerate(messages, start=1):
                    for chunk_no, chunk in enumerate(split_message(message), start=1):
                        try:
                            send_text_message(
                                access_token=wa_token,
                                phone_number_id=wa_phone_number_id,
                                recipient=recipient,
                                body=chunk,
                                api_version=wa_version,
                            )
                            results.append({"type": "agent", "code": code, "recipient": recipient, "message": message_no, "chunk": chunk_no, "status": "sent"})
                            st.write(f"✅ {code} → {recipient} | message {message_no}.{chunk_no}")
                        except Exception as exc:
                            results.append({"type": "agent", "code": code, "recipient": recipient, "message": message_no, "chunk": chunk_no, "status": "error", "error": str(exc)})
                            st.write(f"❌ {code} → {recipient} | {exc}")

            # One overall management report to the two management recipients.
            overall = build_overall_report(snapshot_df)
            for recipient in OVERALL_REPORT_RECIPIENTS:
                for chunk_no, chunk in enumerate(split_message(overall), start=1):
                    try:
                        send_text_message(
                            access_token=wa_token,
                            phone_number_id=wa_phone_number_id,
                            recipient=recipient,
                            body=chunk,
                            api_version=wa_version,
                        )
                        results.append({"type": "overall", "code": "OVERALL", "recipient": recipient, "message": 1, "chunk": chunk_no, "status": "sent"})
                        st.write(f"✅ OVERALL → {recipient} | chunk {chunk_no}")
                    except Exception as exc:
                        results.append({"type": "overall", "code": "OVERALL", "recipient": recipient, "message": 1, "chunk": chunk_no, "status": "error", "error": str(exc)})
                        st.write(f"❌ OVERALL → {recipient} | {exc}")

            errors = [r for r in results if r["status"] == "error"]
            if errors:
                status.update(label=f"Finished with {len(errors)} send error(s)", state="error")
            else:
                status.update(label="All WhatsApp reports sent", state="complete")
        st.dataframe(pd.DataFrame(results), use_container_width=True, hide_index=True)

st.divider()
st.caption(
    "Budget rule: only campaigns with Spend Today > 0 are counted, regardless of current status. "
    "For CBO, campaign daily_budget is counted once. For ABO, only ad sets that spent today are summed."
)
st.caption(
    "Balance note: Meta documents the Ad Account 'balance' field as bill amount due. "
    "If your prepaid Available Funds value differs, use MANUAL_BALANCE_OVERRIDES in config.py until your account exposes a usable balance source."
)
