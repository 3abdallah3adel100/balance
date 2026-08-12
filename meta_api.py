from __future__ import annotations

import json
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
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


def _money_float(value: Any) -> float:
    """Convert Meta numeric values safely; NaN/inf are treated as zero."""
    try:
        if value is None or pd.isna(value):
            return 0.0
        number = float(value)
        return number if math.isfinite(number) else 0.0
    except Exception:
        return 0.0


@dataclass
class MetaClient:
    access_token: str
    api_version: str = "v26.0"
    timeout: int = 60
    discovery_errors: list[str] = field(default_factory=list)

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
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            message = error.get("message") or response.text or "Unknown Meta API error"
            code = error.get("code")
            subcode = error.get("error_subcode")
            extra = []
            if code is not None:
                extra.append(f"code={code}")
            if subcode is not None:
                extra.append(f"subcode={subcode}")
            suffix = f" ({', '.join(extra)})" if extra else ""
            raise MetaAPIError(f"Meta API {response.status_code}{suffix}: {message[:1200]}")
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
        self.discovery_errors = []

        if INCLUDE_ME_AD_ACCOUNTS:
            sources.append(("me/adaccounts", "me/adaccounts", True))

        for business_id in BUSINESS_IDS:
            sources.extend([
                (f"business/{business_id}/owned", f"{business_id}/owned_ad_accounts", False),
                (f"business/{business_id}/client", f"{business_id}/client_ad_accounts", False),
            ])

        fields = "id,account_id,name,account_status,currency,balance,amount_spent,spend_cap,funding_source_details,timezone_name,timezone_offset_hours_utc"
        for source_name, endpoint, is_me in sources:
            try:
                rows = self.fetch_all_pages(endpoint, {"fields": fields, "limit": 500})
            except Exception as exc:
                self.discovery_errors.append(f"{source_name}: {exc}")
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
        sort_cols = [c for c in ["name", "source"] if c in out.columns]
        if sort_cols:
            out = out.sort_values(sort_cols, na_position="last")
        out = out.drop_duplicates("id", keep="first")
        out["clean_account_id"] = out["id"].map(clean_account_id)
        out["buyer_code"] = out["name"].map(extract_buyer_code)
        out["media_buyer"] = out["buyer_code"].map(buyer_name)
        return out.reset_index(drop=True)

    def _get_active_budget_entities(self, account_id: str, entity_type: str) -> tuple[pd.DataFrame, list[str]]:
        """Fetch ACTIVE campaigns/adsets with a fallback if Meta rejects effective_status as a query param.

        This mirrors the working Google Sheets approach:
        /campaigns?...&effective_status=['ACTIVE']
        /adsets?...&effective_status=['ACTIVE']

        We ALSO filter locally by effective_status == ACTIVE so a paused entity can never count.
        """
        if entity_type not in {"campaigns", "adsets"}:
            raise ValueError("entity_type must be campaigns or adsets")

        clean_id = clean_account_id(account_id)
        if entity_type == "campaigns":
            fields = "id,name,status,effective_status,daily_budget,lifetime_budget,budget_remaining"
            limit = 1000
        else:
            fields = "id,name,campaign_id,status,effective_status,daily_budget,lifetime_budget,start_time,end_time"
            limit = 2000

        warnings: list[str] = []
        params = {
            "fields": fields,
            # Same intent as the user's working Apps Script.
            "effective_status": "['ACTIVE']",
            "limit": limit,
        }
        try:
            rows = self.fetch_all_pages(f"act_{clean_id}/{entity_type}", params)
        except Exception as filtered_exc:
            warnings.append(
                f"{entity_type} ACTIVE-filter request failed, retried without server filter: {filtered_exc}"
            )
            rows = self.fetch_all_pages(
                f"act_{clean_id}/{entity_type}",
                {"fields": fields, "limit": limit},
            )

        df = pd.DataFrame(rows)
        if df.empty:
            return df, warnings
        if "effective_status" not in df.columns:
            df["effective_status"] = ""
        df["effective_status"] = df["effective_status"].astype(str).str.upper().str.strip()
        df = df[df["effective_status"] == "ACTIVE"].copy()
        if "id" in df.columns:
            df["id"] = df["id"].astype(str)
        if "campaign_id" in df.columns:
            df["campaign_id"] = df["campaign_id"].astype(str)
        df["account_id"] = clean_id
        return df.reset_index(drop=True), warnings

    def get_campaigns(self, account_id: str) -> pd.DataFrame:
        df, _warnings = self._get_active_budget_entities(account_id, "campaigns")
        return df

    def get_adsets(self, account_id: str) -> pd.DataFrame:
        df, _warnings = self._get_active_budget_entities(account_id, "adsets")
        return df

    def get_active_campaigns(self, account_id: str) -> tuple[pd.DataFrame, list[str]]:
        return self._get_active_budget_entities(account_id, "campaigns")

    def get_active_adsets(self, account_id: str) -> tuple[pd.DataFrame, list[str]]:
        return self._get_active_budget_entities(account_id, "adsets")

    def _today_params_exact(self, day: date) -> dict[str, Any]:
        # Exact query style used by the working Google Apps Script:
        # ?time_range[since]=YYYY-MM-DD&time_range[until]=YYYY-MM-DD
        return {
            "time_range[since]": day.isoformat(),
            "time_range[until]": day.isoformat(),
        }

    def get_entity_today_spend_with_diagnostics(
        self,
        entity_id: str,
        today: date | None = None,
    ) -> tuple[float, str, int, str | None]:
        """Fetch Campaign/Ad Set spend, matching the working Apps Script first.

        Returns (spend, source, rows_count, error). Empty rows are NOT treated as
        an API error; a date_preset=today fallback is tried before returning zero.
        """
        day = today or date.today()
        exact_error: str | None = None
        try:
            params = {"fields": "spend,date_start,date_stop", "limit": 50, **self._today_params_exact(day)}
            rows = self.fetch_all_pages(f"{entity_id}/insights", params)
            if rows:
                spend = float(pd.to_numeric(pd.Series([r.get("spend", 0) for r in rows]), errors="coerce").fillna(0).sum())
                return spend, "entity_exact_time_range", len(rows), None
        except Exception as exc:
            exact_error = str(exc)

        # Fallback uses Meta's own definition of "today" for the ad account.
        try:
            rows = self.fetch_all_pages(
                f"{entity_id}/insights",
                {"fields": "spend,date_start,date_stop", "date_preset": "today", "limit": 50},
            )
            if rows:
                spend = float(pd.to_numeric(pd.Series([r.get("spend", 0) for r in rows]), errors="coerce").fillna(0).sum())
                return spend, "entity_date_preset_today", len(rows), exact_error
            return 0.0, "entity_no_rows", 0, exact_error
        except Exception as fallback_exc:
            combined = f"Exact time_range failed/empty: {exact_error or 'no rows'} | date_preset fallback: {fallback_exc}"
            return 0.0, "entity_spend_error", 0, combined

    def get_entity_today_spend(self, entity_id: str, today: date | None = None) -> float:
        spend, _source, _rows, error = self.get_entity_today_spend_with_diagnostics(entity_id, today)
        if error and _source == "entity_spend_error":
            raise MetaAPIError(error)
        return spend

    def get_level_today_spend_map(
        self,
        account_id: str,
        level: str,
        today: date | None = None,
    ) -> tuple[dict[str, float], str, int, str | None]:
        """Get today's spend for every campaign/adset in one account-level Insights call.

        This is a robust fallback to the per-entity calls used in the old Apps Script.
        """
        if level not in {"campaign", "adset"}:
            raise ValueError("level must be campaign or adset")
        clean_id = clean_account_id(account_id)
        day = today or date.today()
        id_field = "campaign_id" if level == "campaign" else "adset_id"
        name_field = "campaign_name" if level == "campaign" else "adset_name"
        fields = f"{id_field},{name_field},spend,date_start,date_stop"
        if level == "adset":
            fields = f"campaign_id,{fields}"
        exact_error: str | None = None

        def rows_to_map(rows: list[dict]) -> dict[str, float]:
            out: dict[str, float] = {}
            for row in rows:
                entity_id = str(row.get(id_field) or "").strip()
                if not entity_id:
                    continue
                out[entity_id] = out.get(entity_id, 0.0) + _money_float(row.get("spend"))
            return out

        try:
            params = {
                "fields": fields,
                "level": level,
                "limit": 2000,
                **self._today_params_exact(day),
            }
            rows = self.fetch_all_pages(f"act_{clean_id}/insights", params)
            if rows:
                return rows_to_map(rows), f"account_{level}_exact_time_range", len(rows), None
        except Exception as exc:
            exact_error = str(exc)

        try:
            rows = self.fetch_all_pages(
                f"act_{clean_id}/insights",
                {"fields": fields, "level": level, "date_preset": "today", "limit": 2000},
            )
            if rows:
                return rows_to_map(rows), f"account_{level}_date_preset_today", len(rows), exact_error
            return {}, f"account_{level}_no_rows", 0, exact_error
        except Exception as fallback_exc:
            combined = f"{level} exact time_range failed/empty: {exact_error or 'no rows'} | date_preset fallback: {fallback_exc}"
            return {}, f"account_{level}_spend_error", 0, combined

    def get_today_spend_with_diagnostics(
        self,
        account_id: str,
        today: date | None = None,
    ) -> tuple[float, str, str | None]:
        clean_id = clean_account_id(account_id)
        day = today or date.today()
        exact_error: str | None = None

        # Primary: exact same bracket-style date query as the working Apps Script.
        try:
            rows = self.fetch_all_pages(
                f"act_{clean_id}/insights",
                {"fields": "spend,date_start,date_stop", "limit": 50, **self._today_params_exact(day)},
            )
            if rows:
                spend = float(pd.to_numeric(pd.Series([r.get("spend", 0) for r in rows]), errors="coerce").fillna(0).sum())
                if spend > 0:
                    return spend, "account_exact_time_range", None
        except Exception as exc:
            exact_error = str(exc)

        # Fallback: let Meta resolve "today" using the ad account timezone.
        try:
            rows = self.fetch_all_pages(
                f"act_{clean_id}/insights",
                {"fields": "spend,date_start,date_stop", "date_preset": "today", "limit": 50},
            )
            spend = float(pd.to_numeric(pd.Series([r.get("spend", 0) for r in rows]), errors="coerce").fillna(0).sum()) if rows else 0.0
            if spend > 0:
                return spend, "account_date_preset_today", exact_error
            return 0.0, "no_spend_returned", exact_error
        except Exception as fallback_exc:
            combined = f"Account exact time_range failed/empty: {exact_error or 'no rows'} | date_preset fallback: {fallback_exc}"
            return 0.0, "spend_error", combined

    def get_today_spend(self, account_id: str, today: date | None = None) -> float:
        spend, _source, error = self.get_today_spend_with_diagnostics(account_id, today)
        if error and _source == "spend_error":
            raise MetaAPIError(error)
        return spend


def api_money_to_major(value: Any, currency: str) -> float | None:
    """Convert Meta minor-unit money to major units without allowing NaN to poison totals."""
    try:
        if value is None or value == "" or pd.isna(value):
            return None
        raw = float(value)
    except Exception:
        return None
    if not math.isfinite(raw):
        return None
    scale = CURRENCY_MINOR_UNIT_SCALE.get(str(currency or "").upper(), DEFAULT_MINOR_UNIT_SCALE)
    try:
        scale = float(scale)
    except Exception:
        scale = float(DEFAULT_MINOR_UNIT_SCALE)
    if not math.isfinite(scale) or scale <= 0:
        scale = float(DEFAULT_MINOR_UNIT_SCALE)
    return raw / scale


def _parse_display_string_money(display_string: Any, currency: str) -> float | None:
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


def _entity_spend_map(
    client: MetaClient,
    entities: pd.DataFrame,
    today: date | None = None,
    max_workers: int = 12,
    entity_label: str = "entity",
) -> tuple[dict[str, float], list[dict]]:
    """Run fetchTodaySpend(id) for every ACTIVE entity with a daily_budget.

    Unlike the previous version, errors are NOT silently hidden. They are returned
    as diagnostics and later shown in Streamlit + GitHub Actions logs.
    """
    if entities.empty or "id" not in entities.columns:
        return {}, []

    candidates: list[tuple[str, str]] = []
    for _, row in entities.iterrows():
        entity_id = str(row.get("id") or "").strip()
        if not entity_id:
            continue
        effective_status = str(row.get("effective_status") or "").strip().upper()
        if effective_status != "ACTIVE":
            continue
        daily_budget = row.get("daily_budget")
        if daily_budget in (None, "", 0, "0"):
            continue
        candidates.append((entity_id, str(row.get("name") or entity_id)))

    if not candidates:
        return {}, []

    spend_map: dict[str, float] = {}
    errors: list[dict] = []

    def fetch_one(entity_id: str, entity_name: str) -> tuple[str, float, str | None, str]:
        try:
            spend = client.get_entity_today_spend(entity_id, today=today)
            return entity_id, spend, None, entity_name
        except Exception as exc:
            return entity_id, 0.0, str(exc), entity_name

    workers = max(1, min(max_workers, len(candidates)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fetch_one, entity_id, entity_name): (entity_id, entity_name)
            for entity_id, entity_name in candidates
        }
        for future in as_completed(futures):
            entity_id, entity_name = futures[future]
            fetched_id, spend, error, fetched_name = future.result()
            spend_map[fetched_id] = float(spend or 0.0)
            if error:
                errors.append({
                    "entity_type": entity_label,
                    "entity_id": fetched_id,
                    "entity_name": fetched_name or entity_name,
                    "error": error,
                })

    return spend_map, errors


def calculate_account_daily_budget(
    campaigns: pd.DataFrame,
    adsets: pd.DataFrame,
    campaign_spend_by_id: dict[str, float],
    adset_spend_by_id: dict[str, float],
    currency: str,
) -> tuple[float, float, float, list[dict]]:
    """Mirror the Apps Script: campaign budgets and ad-set budgets are independent.

    A budget counts only when ALL are true:
      - effective_status == ACTIVE
      - daily_budget exists and > 0
      - that Campaign/Ad Set itself has Spend Today > 0
    """
    campaign_total = 0.0
    adset_total = 0.0
    details: list[dict] = []

    if not campaigns.empty:
        for _, campaign in campaigns.iterrows():
            campaign_id = str(campaign.get("id") or "")
            status = str(campaign.get("effective_status") or "").strip().upper()
            daily_budget = api_money_to_major(campaign.get("daily_budget"), currency) or 0.0
            spend_today = _money_float(campaign_spend_by_id.get(campaign_id, 0.0))
            if status != "ACTIVE" or daily_budget <= 0 or spend_today <= 0:
                continue
            campaign_total += daily_budget
            details.append({
                "budget_level": "Campaign",
                "campaign_id": campaign_id,
                "campaign_name": str(campaign.get("name") or campaign_id),
                "effective_status": status,
                "spend_today": spend_today,
                "daily_budget": daily_budget,
            })

    if not adsets.empty:
        for _, adset in adsets.iterrows():
            adset_id = str(adset.get("id") or "")
            status = str(adset.get("effective_status") or "").strip().upper()
            daily_budget = api_money_to_major(adset.get("daily_budget"), currency) or 0.0
            spend_today = _money_float(adset_spend_by_id.get(adset_id, 0.0))
            if status != "ACTIVE" or daily_budget <= 0 or spend_today <= 0:
                continue
            adset_total += daily_budget
            details.append({
                "budget_level": "Ad Set",
                "campaign_id": str(adset.get("campaign_id") or ""),
                "adset_id": adset_id,
                "adset_name": str(adset.get("name") or adset_id),
                "effective_status": status,
                "spend_today": spend_today,
                "daily_budget": daily_budget,
            })

    return campaign_total + adset_total, campaign_total, adset_total, details


def fetch_account_snapshot(client: MetaClient, account_row: pd.Series, spend_date: date | None = None) -> dict:
    account_id = clean_account_id(account_row.get("id"))
    account_name = str(account_row.get("name") or account_id)
    currency = str(account_row.get("currency") or "EGP").upper()
    buyer_code = extract_buyer_code(account_name)
    balance, balance_source = get_available_balance(account_row)

    diagnostic_errors: list[dict] = []
    warnings: list[str] = []

    # IMPORTANT: no account-spend gate here.
    # This intentionally mirrors the working Google Sheets script: budgets are
    # fetched and checked independently even if the account-level spend call is empty.
    try:
        campaigns, campaign_fetch_warnings = client.get_active_campaigns(account_id)
        warnings.extend(campaign_fetch_warnings)
        campaign_fetch_status = "OK"
    except Exception as exc:
        campaigns = pd.DataFrame()
        campaign_fetch_status = "ERROR"
        diagnostic_errors.append({
            "entity_type": "campaign_fetch",
            "entity_id": account_id,
            "entity_name": account_name,
            "error": str(exc),
        })

    try:
        adsets, adset_fetch_warnings = client.get_active_adsets(account_id)
        warnings.extend(adset_fetch_warnings)
        adset_fetch_status = "OK"
    except Exception as exc:
        adsets = pd.DataFrame()
        adset_fetch_status = "ERROR"
        diagnostic_errors.append({
            "entity_type": "adset_fetch",
            "entity_id": account_id,
            "entity_name": account_name,
            "error": str(exc),
        })

    # 1) Account spend using the exact Apps Script date syntax first.
    spend_today, spend_source, spend_error = client.get_today_spend_with_diagnostics(
        account_id,
        today=spend_date,
    )
    if spend_error:
        diagnostic_errors.append({
            "entity_type": "account_spend",
            "entity_id": account_id,
            "entity_name": account_name,
            "error": spend_error,
        })

    # 2) Batch spend maps from account Insights. This is much more reliable than
    # depending only on N separate object-level /insights calls.
    campaign_spend_by_id, campaign_spend_source, campaign_spend_rows, campaign_batch_error = (
        client.get_level_today_spend_map(account_id, "campaign", today=spend_date)
    )
    adset_spend_by_id, adset_spend_source, adset_spend_rows, adset_batch_error = (
        client.get_level_today_spend_map(account_id, "adset", today=spend_date)
    )
    if campaign_batch_error and "no rows" not in campaign_batch_error.lower():
        diagnostic_errors.append({
            "entity_type": "campaign_spend_batch",
            "entity_id": account_id,
            "entity_name": account_name,
            "error": campaign_batch_error,
        })
    if adset_batch_error and "no rows" not in adset_batch_error.lower():
        diagnostic_errors.append({
            "entity_type": "adset_spend_batch",
            "entity_id": account_id,
            "entity_name": account_name,
            "error": adset_batch_error,
        })

    # 3) If the batch query did not find spend for active budget entities, fall
    # back to the old Google-Sheets-style direct entity /insights query.
    campaign_candidate_ids = set()
    if not campaigns.empty:
        for _, row in campaigns.iterrows():
            budget_major = api_money_to_major(row.get("daily_budget"), currency)
            if budget_major is not None and budget_major > 0:
                entity_id = str(row.get("id") or "").strip()
                if entity_id:
                    campaign_candidate_ids.add(entity_id)
    adset_candidate_ids = set()
    if not adsets.empty:
        for _, row in adsets.iterrows():
            budget_major = api_money_to_major(row.get("daily_budget"), currency)
            if budget_major is not None and budget_major > 0:
                entity_id = str(row.get("id") or "").strip()
                if entity_id:
                    adset_candidate_ids.add(entity_id)

    campaign_has_candidate_spend = any(
        _money_float(campaign_spend_by_id.get(entity_id, 0)) > 0
        for entity_id in campaign_candidate_ids
    )
    adset_has_candidate_spend = any(
        _money_float(adset_spend_by_id.get(entity_id, 0)) > 0
        for entity_id in adset_candidate_ids
    )

    if campaign_candidate_ids and not campaign_has_candidate_spend:
        direct_map, direct_errors = _entity_spend_map(
            client, campaigns, today=spend_date, entity_label="campaign_spend_direct"
        )
        if any(v > 0 for v in direct_map.values()):
            campaign_spend_by_id.update(direct_map)
            campaign_spend_source += "+direct_entity_fallback"
        diagnostic_errors.extend(direct_errors)

    if adset_candidate_ids and not adset_has_candidate_spend:
        direct_map, direct_errors = _entity_spend_map(
            client, adsets, today=spend_date, entity_label="adset_spend_direct"
        )
        if any(v > 0 for v in direct_map.values()):
            adset_spend_by_id.update(direct_map)
            adset_spend_source += "+direct_entity_fallback"
        diagnostic_errors.extend(direct_errors)

    # Reconstruct account spend if the account-level query is empty but lower
    # levels clearly returned spend. Campaign level is non-overlapping, so prefer it.
    campaign_total_spend = sum(_money_float(v) for v in campaign_spend_by_id.values())
    adset_total_spend = sum(_money_float(v) for v in adset_spend_by_id.values())
    if spend_today <= 0 and campaign_total_spend > 0:
        spend_today = campaign_total_spend
        spend_source = "reconstructed_from_campaign_insights"
    elif spend_today <= 0 and adset_total_spend > 0:
        spend_today = adset_total_spend
        spend_source = "reconstructed_from_adset_insights"

    daily_budget, campaign_daily_budget, adset_daily_budget, budget_details = calculate_account_daily_budget(
        campaigns,
        adsets,
        campaign_spend_by_id,
        adset_spend_by_id,
        currency,
    )

    # A positive account spend with no spend found at campaign/adset level is
    # suspicious. Surface it explicitly instead of pretending the budget is zero.
    if spend_today > 0 and not budget_details:
        warnings.append(
            "Account has Spend Today > 0 but no ACTIVE Campaign/Ad Set with both daily_budget and spend was found."
        )

    coverage_days = balance / daily_budget if balance is not None and daily_budget > 0 else None
    required_for_3_days = max(0.0, daily_budget * 3.0 - balance) if balance is not None and daily_budget > 0 else None

    if diagnostic_errors:
        fetch_status = "ERROR" if campaign_fetch_status == "ERROR" and adset_fetch_status == "ERROR" else "WARNING"
        error_text = " | ".join(
            f"{item['entity_type']}:{item.get('entity_id', '-')}: {item.get('error', '')}"
            for item in diagnostic_errors[:8]
        )
        if len(diagnostic_errors) > 8:
            error_text += f" | +{len(diagnostic_errors) - 8} more error(s)"
    else:
        fetch_status = "OK"
        error_text = None

    return {
        "account_id": account_id,
        "account_name": account_name,
        "currency": currency,
        "buyer_code": buyer_code,
        "media_buyer": buyer_name(buyer_code),
        "spend_today": spend_today,
        "spend_source": spend_source,
        "campaign_spend_source": campaign_spend_source,
        "campaign_spend_rows": campaign_spend_rows,
        "adset_spend_source": adset_spend_source,
        "adset_spend_rows": adset_spend_rows,
        "timezone_name": str(account_row.get("timezone_name") or ""),
        "timezone_offset_hours_utc": account_row.get("timezone_offset_hours_utc"),
        "campaign_daily_budget": campaign_daily_budget,
        "adset_daily_budget": adset_daily_budget,
        "active_daily_budget": daily_budget,  # kept for report compatibility
        "balance": balance,
        "balance_source": balance_source,
        "coverage_days": coverage_days,
        "required_for_3_days": required_for_3_days,
        "active_campaigns_checked": int(len(campaigns)),
        "active_adsets_checked": int(len(adsets)),
        "campaigns_with_spend": int(sum(
            1 for entity_id in set(campaigns.get("id", pd.Series(dtype=str)).astype(str))
            if _money_float(campaign_spend_by_id.get(entity_id, 0.0)) > 0
        )),
        "adsets_with_spend": int(sum(
            1 for entity_id in set(adsets.get("id", pd.Series(dtype=str)).astype(str))
            if _money_float(adset_spend_by_id.get(entity_id, 0.0)) > 0
        )),
        "active_budget_items": len(budget_details),
        "campaign_fetch_status": campaign_fetch_status,
        "adset_fetch_status": adset_fetch_status,
        "fetch_status": fetch_status,
        "error_count": len(diagnostic_errors),
        "error": error_text,
        "budget_details": budget_details,
        "warnings": warnings,
        "diagnostic_errors": diagnostic_errors,
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
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({
                    "account_id": "UNKNOWN",
                    "account_name": "UNKNOWN",
                    "currency": "EGP",
                    "buyer_code": "UNKNOWN",
                    "media_buyer": "Unknown",
                    "spend_today": 0.0,
                    "spend_source": "snapshot_error",
                    "campaign_daily_budget": 0.0,
                    "adset_daily_budget": 0.0,
                    "active_daily_budget": 0.0,
                    "balance": None,
                    "balance_source": "unavailable",
                    "coverage_days": None,
                    "required_for_3_days": None,
                    "active_campaigns_checked": 0,
                    "active_adsets_checked": 0,
                    "campaigns_with_spend": 0,
                    "adsets_with_spend": 0,
                    "active_budget_items": 0,
                    "campaign_fetch_status": "ERROR",
                    "adset_fetch_status": "ERROR",
                    "fetch_status": "ERROR",
                    "error_count": 1,
                    "error": str(exc),
                    "budget_details": [],
                    "warnings": [],
                    "diagnostic_errors": [{
                        "entity_type": "snapshot",
                        "entity_id": "UNKNOWN",
                        "entity_name": "UNKNOWN",
                        "error": str(exc),
                    }],
                })

    detail_rows: list[dict] = []
    simple_rows: list[dict] = []
    for result in results:
        simple = {
            k: v
            for k, v in result.items()
            if k not in {"budget_details", "warnings", "diagnostic_errors"}
        }
        simple["warning_count"] = len(result.get("warnings") or [])
        simple_rows.append(simple)

        for item in result.get("budget_details") or []:
            detail_rows.append({
                "row_type": "budget",
                "account_id": result.get("account_id"),
                "account_name": result.get("account_name"),
                "buyer_code": result.get("buyer_code"),
                **item,
            })
        for item in result.get("diagnostic_errors") or []:
            detail_rows.append({
                "row_type": "ERROR",
                "account_id": result.get("account_id"),
                "account_name": result.get("account_name"),
                "buyer_code": result.get("buyer_code"),
                **item,
            })
        for warning in result.get("warnings") or []:
            detail_rows.append({
                "row_type": "WARNING",
                "account_id": result.get("account_id"),
                "account_name": result.get("account_name"),
                "buyer_code": result.get("buyer_code"),
                "error": warning,
            })

    df = pd.DataFrame(simple_rows)
    if not df.empty:
        df = df.sort_values(["buyer_code", "account_name"]).reset_index(drop=True)
    return df, detail_rows
