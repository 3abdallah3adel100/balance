from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd
import requests

from config import (
    BUSINESS_IDS,
    CURRENCY_MINOR_UNIT_SCALE,
    DEFAULT_MINOR_UNIT_SCALE,
    INCLUDE_ME_AD_ACCOUNTS,
    MANUAL_BALANCE_OVERRIDES,
    ME_ACCOUNT_NAME_PREFIXES,
    clean_account_id,
    extract_buyer_code,
    buyer_name,
)

BASE_URL = "https://graph.facebook.com"


class MetaAPIError(RuntimeError):
    pass


@dataclass
class MetaClient:
    access_token: str
    api_version: str = "v26.0"
    timeout: int = 60

    def _get(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict:
        url = path_or_url if path_or_url.startswith("http") else f"{BASE_URL}/{self.api_version}/{path_or_url.lstrip('/')}"
        request_params = dict(params or {})
        request_params.setdefault("access_token", self.access_token)
        response = requests.get(url, params=request_params, timeout=self.timeout)
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if not response.ok:
            message = payload.get("error", {}).get("message") or response.text
            raise MetaAPIError(f"Meta API {response.status_code}: {message[:1000]}")
        return payload

    def fetch_all_pages(self, path_or_url: str, params: dict[str, Any] | None = None) -> list[dict]:
        rows: list[dict] = []
        url = path_or_url
        page_params = dict(params or {})
        while True:
            payload = self._get(url, page_params)
            rows.extend(payload.get("data", []))
            next_url = payload.get("paging", {}).get("next")
            if not next_url:
                break
            url = next_url
            page_params = None
        return rows

    def get_ad_accounts(self) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        sources: list[tuple[str, str, bool]] = []

        if INCLUDE_ME_AD_ACCOUNTS:
            sources.append(("me/adaccounts", "me/adaccounts", True))

        for business_id in BUSINESS_IDS:
            sources.extend([
                (f"business/{business_id}/owned", f"{business_id}/owned_ad_accounts", False),
                (f"business/{business_id}/client", f"{business_id}/client_ad_accounts", False),
            ])

        fields = "id,account_id,name,account_status,currency,balance,amount_spent,spend_cap,funding_source_details"
        for source_name, endpoint, is_me in sources:
            try:
                rows = self.fetch_all_pages(endpoint, {"fields": fields, "limit": 500})
            except Exception:
                continue
            if not rows:
                continue
            df = pd.DataFrame(rows)
            df["source"] = source_name
            if is_me and "name" in df.columns:
                mask = df["name"].astype(str).str.upper().apply(
                    lambda name: any(prefix in name for prefix in ME_ACCOUNT_NAME_PREFIXES)
                )
                df = df[mask].copy()
            if not df.empty:
                frames.append(df)

        if not frames:
            return pd.DataFrame()

        out = pd.concat(frames, ignore_index=True)
        if "id" not in out.columns:
            return pd.DataFrame()
        out = out.sort_values(["name", "source"], na_position="last").drop_duplicates("id", keep="first")
        out["clean_account_id"] = out["id"].map(clean_account_id)
        out["buyer_code"] = out["name"].map(extract_buyer_code)
        out["media_buyer"] = out["buyer_code"].map(buyer_name)
        return out.reset_index(drop=True)

    def get_campaigns(self, account_id: str) -> pd.DataFrame:
        clean_id = clean_account_id(account_id)
        fields = "id,name,status,effective_status,daily_budget,lifetime_budget,budget_remaining"
        rows = self.fetch_all_pages(
            f"act_{clean_id}/campaigns",
            {"fields": fields, "limit": 1000},
        )
        df = pd.DataFrame(rows)
        if not df.empty:
            df["account_id"] = clean_id
        return df

    def get_adsets(self, account_id: str) -> pd.DataFrame:
        clean_id = clean_account_id(account_id)
        fields = "id,name,campaign_id,status,effective_status,daily_budget,lifetime_budget,start_time,end_time"
        rows = self.fetch_all_pages(
            f"act_{clean_id}/adsets",
            {"fields": fields, "limit": 2000},
        )
        df = pd.DataFrame(rows)
        if not df.empty:
            df["account_id"] = clean_id
        return df

    def get_today_spend(self, account_id: str, today: date | None = None) -> float:
        clean_id = clean_account_id(account_id)
        day = today or date.today()
        rows = self.fetch_all_pages(
            f"act_{clean_id}/insights",
            {
                "fields": "spend",
                "level": "account",
                "time_range": f'{{"since":"{day.isoformat()}","until":"{day.isoformat()}"}}',
                "limit": 50,
            },
        )
        if not rows:
            return 0.0
        return float(pd.to_numeric(pd.Series([r.get("spend", 0) for r in rows]), errors="coerce").fillna(0).sum())

    def get_today_spend_breakdown(
        self,
        account_id: str,
        level: str,
        today: date | None = None,
    ) -> pd.DataFrame:
        """Return only entities that actually spent today.

        level='campaign' returns campaign_id/campaign_name/spend.
        level='adset' returns campaign_id/adset_id/adset_name/spend.
        Current ACTIVE/PAUSED status is intentionally NOT used here.
        """
        if level not in {"campaign", "adset"}:
            raise ValueError("level must be 'campaign' or 'adset'")

        clean_id = clean_account_id(account_id)
        day = today or date.today()
        fields = "spend,campaign_id,campaign_name"
        if level == "adset":
            fields += ",adset_id,adset_name"

        rows = self.fetch_all_pages(
            f"act_{clean_id}/insights",
            {
                "fields": fields,
                "level": level,
                "time_range": f'{{"since":"{day.isoformat()}","until":"{day.isoformat()}"}}',
                "limit": 2000,
            },
        )
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        if "spend" not in df.columns:
            return pd.DataFrame()
        df["spend"] = pd.to_numeric(df["spend"], errors="coerce").fillna(0.0)
        df = df[df["spend"] > 0].copy()
        for col in ["campaign_id", "adset_id"]:
            if col in df.columns:
                df[col] = df[col].astype(str)
        return df.reset_index(drop=True)


def api_money_to_major(value: Any, currency: str) -> float | None:
    if value in (None, ""):
        return None
    try:
        raw = float(value)
    except Exception:
        return None
    scale = CURRENCY_MINOR_UNIT_SCALE.get(str(currency or "").upper(), DEFAULT_MINOR_UNIT_SCALE)
    return raw / scale


def _parse_display_string_money(display_string: Any, currency: str) -> float | None:
    """Only parse display_string if it clearly contains the account currency or a money symbol.

    This intentionally avoids treating masked card digits as a balance.
    """
    import re

    text = str(display_string or "").strip()
    if not text:
        return None
    currency_upper = str(currency or "").upper()
    looks_monetary = (
        currency_upper and currency_upper in text.upper()
    ) or any(symbol in text for symbol in ["$", "€", "£", "ج.م", "EGP"])
    if not looks_monetary:
        return None
    matches = re.findall(r"-?[\d][\d,]*(?:\.\d+)?", text)
    if not matches:
        return None
    try:
        return float(matches[-1].replace(",", ""))
    except Exception:
        return None


def get_available_balance(account_row: pd.Series) -> tuple[float | None, str]:
    """Resolve the balance used for coverage calculations.

    Priority:
    1) manual override (explicit usable balance)
    2) clearly monetary funding_source_details.display_string
    3) Meta Ad Account `balance` field fallback

    Meta documents `balance` as bill amount due. Therefore prepaid/available-funds
    accounts may require an override if the API value does not match Ads Manager.
    """
    clean_id = clean_account_id(account_row.get("id") or account_row.get("account_id"))
    override = MANUAL_BALANCE_OVERRIDES.get(clean_id)
    if override is None:
        override = MANUAL_BALANCE_OVERRIDES.get(f"act_{clean_id}")
    if override is not None:
        return float(override), "manual_override"

    currency = str(account_row.get("currency") or "EGP")
    fsd = account_row.get("funding_source_details")
    if isinstance(fsd, dict):
        parsed = _parse_display_string_money(fsd.get("display_string"), currency)
        if parsed is not None:
            return parsed, "funding_source_display"

    fallback = api_money_to_major(account_row.get("balance"), currency)
    if fallback is not None:
        return fallback, "meta_balance_field"
    return None, "unavailable"


def calculate_account_daily_budget(
    campaigns: pd.DataFrame,
    adsets: pd.DataFrame,
    campaign_spend: pd.DataFrame,
    adset_spend: pd.DataFrame,
    currency: str,
) -> tuple[float, list[dict], list[str]]:
    """Calculate budget ONLY for campaigns/ad sets that spent today.

    Rule requested for this project:
    - Campaign must have Spend Today > 0.
    - Current campaign status does not matter (ACTIVE/PAUSED/etc.).
    - CBO: use the campaign daily_budget once.
    - ABO: sum only ad sets inside that campaign that themselves spent today.
    """
    if campaigns.empty or campaign_spend.empty:
        return 0.0, [], []

    campaigns = campaigns.copy()
    campaigns["id"] = campaigns.get("id", pd.Series(dtype=str)).astype(str)
    if "effective_status" not in campaigns.columns:
        campaigns["effective_status"] = ""
    campaigns["effective_status"] = campaigns["effective_status"].astype(str).str.upper()

    campaign_spend = campaign_spend.copy()
    campaign_spend["campaign_id"] = campaign_spend.get("campaign_id", pd.Series(dtype=str)).astype(str)
    campaign_spend["spend"] = pd.to_numeric(campaign_spend.get("spend", 0), errors="coerce").fillna(0.0)
    campaign_spend = campaign_spend[campaign_spend["spend"] > 0].copy()
    spend_by_campaign = campaign_spend.groupby("campaign_id", dropna=False)["spend"].sum().to_dict()

    if not adsets.empty:
        adsets = adsets.copy()
        adsets["id"] = adsets.get("id", pd.Series(dtype=str)).astype(str)
        adsets["campaign_id"] = adsets.get("campaign_id", pd.Series(dtype=str)).astype(str)
        if "effective_status" not in adsets.columns:
            adsets["effective_status"] = ""
        adsets["effective_status"] = adsets["effective_status"].astype(str).str.upper()

    spent_adset_ids: set[str] = set()
    spend_by_adset: dict[str, float] = {}
    if not adset_spend.empty:
        adset_spend = adset_spend.copy()
        adset_spend["adset_id"] = adset_spend.get("adset_id", pd.Series(dtype=str)).astype(str)
        adset_spend["campaign_id"] = adset_spend.get("campaign_id", pd.Series(dtype=str)).astype(str)
        adset_spend["spend"] = pd.to_numeric(adset_spend.get("spend", 0), errors="coerce").fillna(0.0)
        adset_spend = adset_spend[adset_spend["spend"] > 0].copy()
        spent_adset_ids = set(adset_spend["adset_id"].astype(str))
        spend_by_adset = adset_spend.groupby("adset_id", dropna=False)["spend"].sum().to_dict()

    total = 0.0
    details: list[dict] = []
    warnings: list[str] = []

    spent_campaign_ids = set(spend_by_campaign)
    spent_campaigns = campaigns[campaigns["id"].isin(spent_campaign_ids)].copy()

    missing_campaign_ids = spent_campaign_ids - set(spent_campaigns["id"].astype(str))
    for campaign_id in sorted(missing_campaign_ids):
        warnings.append(
            f"Campaign {campaign_id} spent today but was not returned by the campaigns endpoint, so its daily budget could not be counted."
        )

    for _, campaign in spent_campaigns.iterrows():
        campaign_id = str(campaign.get("id") or "")
        campaign_name = str(campaign.get("name") or campaign_id)
        campaign_status = str(campaign.get("effective_status") or "")
        campaign_spend_value = float(spend_by_campaign.get(campaign_id, 0.0) or 0.0)
        campaign_daily = api_money_to_major(campaign.get("daily_budget"), currency) or 0.0

        # CBO: campaign has its own daily budget. Count it once if it spent today.
        if campaign_daily > 0:
            total += campaign_daily
            details.append({
                "campaign_id": campaign_id,
                "campaign_name": campaign_name,
                "campaign_status": campaign_status,
                "spend_today": campaign_spend_value,
                "budget_level": "Campaign",
                "daily_budget": campaign_daily,
            })
            continue

        # ABO: only ad sets that actually spent today are counted.
        campaign_adsets = (
            adsets[
                (adsets["campaign_id"] == campaign_id)
                & (adsets["id"].isin(spent_adset_ids))
            ].copy()
            if not adsets.empty
            else pd.DataFrame()
        )

        adset_total = 0.0
        if not campaign_adsets.empty:
            for _, adset in campaign_adsets.iterrows():
                adset_id = str(adset.get("id") or "")
                adset_daily = api_money_to_major(adset.get("daily_budget"), currency) or 0.0
                if adset_daily <= 0:
                    continue
                adset_total += adset_daily
                details.append({
                    "campaign_id": campaign_id,
                    "campaign_name": campaign_name,
                    "campaign_status": campaign_status,
                    "spend_today": campaign_spend_value,
                    "budget_level": "Ad Set",
                    "adset_id": adset_id,
                    "adset_name": str(adset.get("name") or ""),
                    "adset_status": str(adset.get("effective_status") or ""),
                    "adset_spend_today": float(spend_by_adset.get(adset_id, 0.0) or 0.0),
                    "daily_budget": adset_daily,
                })

        if adset_total > 0:
            total += adset_total
        else:
            warnings.append(
                f"Campaign '{campaign_name}' ({campaign_id}) spent {campaign_spend_value:.2f} today but no spending ad set with a daily budget was found."
            )

    return total, details, warnings


def fetch_account_snapshot(client: MetaClient, account_row: pd.Series, spend_date: date | None = None) -> dict:
    account_id = clean_account_id(account_row.get("id"))
    account_name = str(account_row.get("name") or account_id)
    currency = str(account_row.get("currency") or "EGP").upper()
    buyer_code = extract_buyer_code(account_name)

    try:
        # Spend-first gate: if the account did not spend today, do NOT fetch
        # campaigns/ad sets for budget calculations. This removes no-fund / idle
        # active campaigns from the daily-budget report entirely.
        spend_today = client.get_today_spend(account_id, today=spend_date)
        balance, balance_source = get_available_balance(account_row)

        if spend_today <= 0:
            return {
                "account_id": account_id,
                "account_name": account_name,
                "currency": currency,
                "buyer_code": buyer_code,
                "media_buyer": buyer_name(buyer_code),
                "spend_today": 0.0,
                "active_daily_budget": 0.0,  # legacy column name; means spending daily budget
                "balance": balance,
                "balance_source": balance_source,
                "coverage_days": None,
                "required_for_3_days": None,
                "active_budget_items": 0,
                "budget_details": [],
                "warnings": [],
                "error": None,
            }

        campaign_spend = client.get_today_spend_breakdown(account_id, "campaign", today=spend_date)
        campaigns = client.get_campaigns(account_id)

        # Only fetch ad sets when at least one spending campaign does not have
        # a campaign-level daily budget (ABO case).
        adsets = pd.DataFrame()
        adset_spend = pd.DataFrame()
        if not campaign_spend.empty and not campaigns.empty:
            spent_ids = set(campaign_spend["campaign_id"].astype(str))
            candidate_campaigns = campaigns[campaigns["id"].astype(str).isin(spent_ids)].copy()
            needs_adsets = False
            for _, campaign in candidate_campaigns.iterrows():
                if (api_money_to_major(campaign.get("daily_budget"), currency) or 0.0) <= 0:
                    needs_adsets = True
                    break
            if needs_adsets:
                adsets = client.get_adsets(account_id)
                adset_spend = client.get_today_spend_breakdown(account_id, "adset", today=spend_date)

        daily_budget, budget_details, warnings = calculate_account_daily_budget(
            campaigns,
            adsets,
            campaign_spend,
            adset_spend,
            currency,
        )

        coverage_days = None
        if balance is not None and daily_budget > 0:
            coverage_days = balance / daily_budget
        required_for_3_days = None
        if balance is not None and daily_budget > 0:
            required_for_3_days = max(0.0, daily_budget * 3.0 - balance)

        return {
            "account_id": account_id,
            "account_name": account_name,
            "currency": currency,
            "buyer_code": buyer_code,
            "media_buyer": buyer_name(buyer_code),
            "spend_today": spend_today,
            "active_daily_budget": daily_budget,  # legacy column name; means spending daily budget
            "balance": balance,
            "balance_source": balance_source,
            "coverage_days": coverage_days,
            "required_for_3_days": required_for_3_days,
            "active_budget_items": len(budget_details),
            "budget_details": budget_details,
            "warnings": warnings,
            "error": None,
        }
    except Exception as exc:
        return {
            "account_id": account_id,
            "account_name": account_name,
            "currency": currency,
            "buyer_code": buyer_code,
            "media_buyer": buyer_name(buyer_code),
            "spend_today": 0.0,
            "active_daily_budget": 0.0,
            "balance": None,
            "balance_source": "unavailable",
            "coverage_days": None,
            "required_for_3_days": None,
            "active_budget_items": 0,
            "budget_details": [],
            "warnings": [],
            "error": str(exc),
        }


def fetch_full_snapshot(
    client: MetaClient,
    accounts_df: pd.DataFrame,
    max_workers: int = 8,
    spend_date: date | None = None,
) -> tuple[pd.DataFrame, list[dict]]:
    if accounts_df.empty:
        return pd.DataFrame(), []
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 16))) as executor:
        futures = [
            executor.submit(fetch_account_snapshot, client, row, spend_date)
            for _, row in accounts_df.iterrows()
        ]
        for future in as_completed(futures):
            results.append(future.result())

    detail_rows: list[dict] = []
    simple_rows: list[dict] = []
    for result in results:
        simple = {k: v for k, v in result.items() if k not in {"budget_details", "warnings"}}
        simple["warning_count"] = len(result.get("warnings") or [])
        simple_rows.append(simple)
        for item in result.get("budget_details") or []:
            detail_rows.append({
                "account_id": result["account_id"],
                "account_name": result["account_name"],
                "buyer_code": result["buyer_code"],
                **item,
            })
    df = pd.DataFrame(simple_rows)
    if not df.empty:
        df = df.sort_values(["buyer_code", "account_name"]).reset_index(drop=True)
    return df, detail_rows
