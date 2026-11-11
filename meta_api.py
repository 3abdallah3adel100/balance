from __future__ import annotations

import json
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from threading import Lock
import time
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

    # The same client is shared across all account workers for one Meta token.
    # Pace requests and share the cooldown so a rate-limit response from one
    # worker slows the other workers instead of causing a retry storm.
    _pace_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _next_request_at: float = field(default=0.0, init=False, repr=False)
    min_request_spacing: float = 0.18
    max_rate_limit_retries: int = 2
    _rate_limited_out: bool = field(default=False, init=False, repr=False)

    def _reserve_request_slot(self) -> None:
        with self._pace_lock:
            request_at = max(time.monotonic(), self._next_request_at)
            self._next_request_at = request_at + max(0.0, self.min_request_spacing)
        delay = request_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def _rate_limit_cooldown(self, seconds: float) -> None:
        with self._pace_lock:
            self._next_request_at = max(self._next_request_at, time.monotonic() + seconds)

    def _get(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict:
        url = path_or_url if path_or_url.startswith("http") else f"{BASE_URL}/{self.api_version}/{path_or_url.lstrip('/')}"
        request_params = dict(params or {})
        request_params.setdefault("access_token", self.access_token)
        for attempt in range(self.max_rate_limit_retries + 1):
            # A permanently exhausted token should not be retried for every
            # remaining account; doing so turns a fast failure into minutes.
            with self._pace_lock:
                if self._rate_limited_out:
                    raise MetaAPIError("Meta API rate limit exhausted for this token (code=17); reports not sent")
            self._reserve_request_slot()
            with self._pace_lock:
                if self._rate_limited_out:
                    raise MetaAPIError("Meta API rate limit exhausted for this token (code=17); reports not sent")
            response = requests.get(url, params=request_params, timeout=self.timeout)
            try:
                payload = response.json()
            except Exception:
                payload = {}
            if response.ok:
                return payload
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            if not isinstance(error, dict):
                error = {}
            message = error.get("message") or response.text or "Unknown Meta API error"
            code = error.get("code")
            subcode = error.get("error_subcode")
            extra = []
            if code is not None:
                extra.append(f"code={code}")
            if subcode is not None:
                extra.append(f"subcode={subcode}")
            suffix = f" ({', '.join(extra)})" if extra else ""
            # Meta commonly returns HTTP 400 + code 17 for a *temporary* rate
            # limit. Never retry permission errors (code 200), which need a
            # different authorized token instead.
            retryable = (code in (4, 17, 32, 613, 80004)
                         or response.status_code in (429, 500, 502, 503, 504))
            # Hard quota errors require Meta's quota window to recover. A
            # 3/6-second retry burns more quota and multiplies latency.
            hard_quota = code in (4, 17, 32, 613, 80000, 80004) or response.status_code == 429
            if retryable and not hard_quota and attempt < self.max_rate_limit_retries:
                try:
                    server_delay = float(response.headers.get("Retry-After", 0))
                except (TypeError, ValueError, AttributeError):
                    server_delay = 0.0
                backoff = min(15.0, max(3.0 * (2 ** attempt), server_delay))
                self._rate_limit_cooldown(backoff)
                print(f"Meta API temporary limit/error (code={code}, http={response.status_code}); "
                      f"retry {attempt + 1}/{self.max_rate_limit_retries} after cooldown")
                continue
            if hard_quota:
                with self._pace_lock:
                    self._rate_limited_out = True
            raise MetaAPIError(f"Meta API {response.status_code}{suffix}: {str(message)[:1200]}")
        raise MetaAPIError("Meta API retries exhausted")

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

    def get_today_spend_maps(self, account_id: str, today: date | None = None):
        """One ad-level Insights query: spends are never added at multiple levels.

        Ad-level records are essential to distinguish a PAUSED ad within a
        still-active ad set. Aggregate campaign, adset and account spend from
        those SAME unique ad rows so CBO/ABO/ads cannot duplicate spend.
        """
        clean_id = clean_account_id(account_id)
        day = today or date.today()
        fields = "ad_id,adset_id,campaign_id,spend,date_start,date_stop"
        # Exact date is authoritative; zero rows means zero spend for that
        # date, not a reason to make a second, potentially different-day query.
        # API failures propagate; NEVER convert an error into a zero balance.
        params = {"fields": fields, "level": "ad", "limit": 2000,
                  **self._today_params_exact(day)}
        rows = self.fetch_all_pages(f"act_{clean_id}/insights", params)
        source = "account_ad_exact_time_range"
        by_campaign: dict[str, float] = {}
        by_adset: dict[str, float] = {}
        ad_spend_rows: list[dict] = []
        seen = set()
        total = 0.0
        for item in rows:
            spent = _money_float(item.get("spend"))
            if spent <= 0:
                continue
            aid = str(item.get("ad_id") or "").strip()
            cid = str(item.get("campaign_id") or "").strip()
            sid = str(item.get("adset_id") or "").strip()
            if not aid or not cid or not sid:
                raise MetaAPIError(f"Incomplete ad/campaign/adset IDs in Insights for act_{clean_id}")
            if aid in seen:
                raise MetaAPIError(f"Duplicate ad_id in Insights for act_{clean_id}: {aid}; refusing inflated totals")
            seen.add(aid)
            by_campaign[cid] = by_campaign.get(cid, 0.0) + spent
            by_adset[sid] = by_adset.get(sid, 0.0) + spent
            ad_spend_rows.append(dict(ad_id=aid, adset_id=sid, campaign_id=cid, spend=spent))
            total += spent
        return total, by_campaign, by_adset, source, len(rows), ad_spend_rows

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


def calculate_account_daily_budget(campaigns, adsets, campaign_spend_by_id,
                                   adset_spend_by_id, currency, ads, ad_spend_rows=None):
    from budget_policy import allocation_budget_breakdown
    live_total, stopped_total, details = allocation_budget_breakdown(
        campaigns.to_dict('records'), adsets.to_dict('records'), ads,
        campaign_spend_by_id, adset_spend_by_id,
        lambda value: api_money_to_major(value, currency),
        ad_spend_rows=ad_spend_rows)
    print('BUDGET CLOSING: ' + json.dumps(details, ensure_ascii=False))
    campaign_total = sum(x['daily_budget'] for x in details if x['budget_level'] == 'CBO')
    adset_total = sum(x['daily_budget'] for x in details if x['budget_level'] == 'ABO')
    return live_total + stopped_total, campaign_total, adset_total, details


def fetch_account_snapshot(client: MetaClient, account_row: pd.Series, spend_date: date | None = None) -> dict:
    account_id = clean_account_id(account_row.get("id"))
    account_name = str(account_row.get("name") or account_id)
    currency = str(account_row.get("currency") or "EGP").upper()
    buyer_code = extract_buyer_code(account_name)
    balance, balance_source = get_available_balance(account_row)

    diagnostic_errors: list[dict] = []
    warnings: list[str] = []
    campaign_fetch_status = adset_fetch_status = 'OK'
    ad_spend_rows = None
    use_bulk_spend = callable(getattr(client, "get_today_spend_maps", None))

    if use_bulk_spend:
        # First, one authoritative ad-level Insights read. If there was no
        # spend today, the original business rule counts ZERO budget: we do
        # not need to enumerate every campaign, ad set, or ad to prove it.
        spend_result = client.get_today_spend_maps(account_id, spend_date)
        (spend_today, campaign_spend_by_id, adset_spend_by_id,
         spend_source, spend_rows) = spend_result[:5]
        if len(spend_result) >= 6:
            ad_spend_rows = spend_result[5]
        campaign_spend_source = adset_spend_source = spend_source
        campaign_spend_rows = adset_spend_rows = spend_rows
        if spend_today <= 0:
            print('BUDGET AUDIT: ' + json.dumps({
                'account_id': account_id,
                'overall_daily_budget': 0,
                'live_daily_budget': 0,
                'stopped_spend_today': 0,
                'live_owners': 0,
                'stopped_ads': 0,
                'total_spend_today': 0,
                'inventory_skipped_no_spend': True,
            }, ensure_ascii=False))
            return {
                'account_id': account_id, 'account_name': account_name,
                'currency': currency, 'buyer_code': buyer_code,
                'media_buyer': buyer_name(buyer_code), 'spend_today': 0.0,
                'spend_source': spend_source, 'campaign_spend_source': spend_source,
                'campaign_spend_rows': spend_rows, 'adset_spend_source': spend_source,
                'adset_spend_rows': spend_rows,
                'timezone_name': str(account_row.get('timezone_name') or ''),
                'timezone_offset_hours_utc': account_row.get('timezone_offset_hours_utc'),
                'campaign_daily_budget': 0.0, 'adset_daily_budget': 0.0,
                'active_daily_budget': 0.0, 'live_daily_budget': 0.0,
                'stopped_spend_today': 0.0, 'balance': balance,
                'balance_source': balance_source, 'coverage_days': None,
                'required_for_3_days': None, 'active_campaigns_checked': 0,
                'active_adsets_checked': 0, 'campaigns_with_spend': 0,
                'adsets_with_spend': 0, 'active_budget_items': 0,
                'campaign_fetch_status': 'SKIPPED_NO_SPEND',
                'adset_fetch_status': 'SKIPPED_NO_SPEND',
                'fetch_status': 'OK', 'error_count': 0, 'error': None,
                'budget_details': [], 'warnings': [], 'diagnostic_errors': [],
            }

    # Inventory is only needed for accounts that actually spent today, or
    # the legacy test/client path that cannot supply the one-call bulk result.
    campaigns = pd.DataFrame(client.fetch_all_pages(f'act_{account_id}/campaigns',
        {'fields': 'id,name,effective_status,daily_budget,lifetime_budget', 'limit': 500}))
    adsets = pd.DataFrame(client.fetch_all_pages(f'act_{account_id}/adsets',
        {'fields': 'id,name,campaign_id,effective_status,daily_budget,lifetime_budget', 'limit': 500}))
    ads = client.fetch_all_pages(f'act_{account_id}/ads',
        {'fields': 'id,campaign_id,adset_id,effective_status', 'limit': 500})

    if not use_bulk_spend:
        # Compatibility with custom MetaClient implementations and tests.
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

    # Spent ads may be archived and absent from account edges. Recover their
    # parent / status to avoid counting the same spend at multiple levels.
    # This normally makes ZERO extra calls (all spend IDs exist in the edges).
    if ad_spend_rows is not None:
        known_adsets = {str(x.get('id') or '') for x in adsets.to_dict('records')}
        for sid in {str(x['adset_id']) for x in ad_spend_rows} - known_adsets:
            parent = client._get(sid, {'fields': 'id,name,campaign_id,effective_status,daily_budget,lifetime_budget'})
            if str(parent.get('id') or '') != sid:
                raise MetaAPIError('Could not recover spent ad set ' + sid)
            adsets = pd.concat([adsets, pd.DataFrame([parent])], ignore_index=True)
        known_ads = {str(x.get('id') or '') for x in ads}
        adset_lookup = {str(x['id']): x for x in adsets.to_dict('records')}
        campaign_lookup = {str(x['id']): x for x in campaigns.to_dict('records')}
        for metric in ad_spend_rows:
            aid = str(metric['ad_id'])
            if aid in known_ads:
                continue
            group = adset_lookup.get(str(metric['adset_id']), {})
            campaign = campaign_lookup.get(str(metric['campaign_id']), {})
            # A paused ancestor proves the ad cannot be live. Do not waste a
            # Meta request recovering archived ad metadata in this case.
            if (str(group.get('effective_status') or '').upper() != 'ACTIVE'
                or (campaign and str(campaign.get('effective_status') or '').upper() != 'ACTIVE')):
                continue
            try:
                ad = client._get(aid, {'fields': 'id,campaign_id,adset_id,effective_status'})
            except Exception as exc:
                raise MetaAPIError(f'Missing status for spent ad {aid} under ACTIVE parents: {exc}')
            if str(ad.get('id') or '') != aid:
                raise MetaAPIError('Unexpected ad metadata for ' + aid)
            ads.append(ad)
            known_ads.add(aid)

    # Account edges may omit archived/deleted parents. Only recover parents
    # needed for an active child or an ad set that actually spent today.
    known_campaigns = set(campaigns.get('id', pd.Series(dtype=str)).astype(str))
    active_sets = {str(ad.get('adset_id') or '') for ad in ads if ad.get('effective_status') == 'ACTIVE'}
    for adset in adsets.to_dict('records'):
        sid, cid = str(adset['id']), str(adset.get('campaign_id') or '')
        if cid in known_campaigns or (sid not in active_sets and _money_float(adset_spend_by_id.get(sid, 0)) == 0):
            continue
        if not cid:
            raise MetaAPIError('Missing campaign_id for relevant ad set ' + sid)
        parent = client._get(cid, {'fields': 'id,name,effective_status,daily_budget,lifetime_budget'})
        if str(parent.get('id')) != cid:
            raise MetaAPIError('Invalid campaign metadata for ' + cid)
        campaigns = pd.concat([campaigns, pd.DataFrame([parent])], ignore_index=True)
        known_campaigns.add(cid)

    daily_budget, campaign_daily_budget, adset_daily_budget, budget_details = calculate_account_daily_budget(
        campaigns,
        adsets,
        campaign_spend_by_id,
        adset_spend_by_id,
        currency,
        ads,
        ad_spend_rows=ad_spend_rows,
    )

    # Live budget is ONLY the current effective budget of running owners.
    # "spent" owners are intentionally kept in active_daily_budget (closing
    # total), but excluded from the new Overall Live Budget metric.
    live_daily_budget = sum(
        _money_float(item.get("daily_budget"))
        for item in budget_details
        if item.get("calculation_mode") == "live"
    )
    stopped_spend_today = sum(
        _money_float(item.get("daily_budget"))
        for item in budget_details
        if item.get("calculation_mode") == "spent"
    )
    print('BUDGET AUDIT: ' + json.dumps({
        'account_id': account_id,
        'overall_daily_budget': round(daily_budget, 2),
        'live_daily_budget': round(live_daily_budget, 2),
        'stopped_spend_today': round(stopped_spend_today, 2),
        'live_owners': sum(x['calculation_mode'] == 'live' for x in budget_details),
        'stopped_ads': sum(x['calculation_mode'] == 'spent' for x in budget_details),
        'total_spend_today': round(spend_today, 2),
    }, ensure_ascii=False))

    # A positive account spend with no spend found at campaign/adset level is
    # suspicious. Surface it explicitly instead of pretending the budget is zero.
    if spend_today > 0 and not budget_details:
        warnings.append(
            "Account has Spend Today > 0 but no ACTIVE Campaign/Ad Set with both daily_budget and spend was found."
        )

    # Coverage is operational runway. Stopped delivery does not continue spending.
    coverage_days = balance / live_daily_budget if balance is not None and live_daily_budget > 0 else None
    required_for_3_days = max(0.0, live_daily_budget * 3.0 - balance) if balance is not None and live_daily_budget > 0 else None

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
        "active_daily_budget": daily_budget,  # closing budget: live + stopped spend
        "live_daily_budget": live_daily_budget,  # running budgets only
        "stopped_spend_today": stopped_spend_today,
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
    clients_by_token_key: dict[str, MetaClient] | None = None,
) -> tuple[pd.DataFrame, list[dict]]:
    if accounts_df.empty:
        return pd.DataFrame(), []

    token_clients = clients_by_token_key or {"token_1": client}

    def fetch_with_correct_token(row: pd.Series) -> dict:
        preferred = str(row.get("_token_key") or "token_1")
        ordered_keys = [preferred] + [k for k in token_clients if k != preferred]
        first_result = None
        for key in ordered_keys:
            selected_client = token_clients.get(key)
            if selected_client is None:
                continue
            try:
                result = fetch_account_snapshot(selected_client, row, spend_date)
            except Exception:
                if first_result is None and len(ordered_keys) == 1:
                    raise
                continue
            if first_result is None:
                first_result = result
            # Only recheck if the account was completely unavailable with the
            # first token. Do not double-count by returning multiple snapshots.
            if result.get("fetch_status") != "ERROR":
                return result
        if first_result is not None:
            return first_result
        raise MetaAPIError(f"Could not fetch Allocation for {row.get('id', 'UNKNOWN')} with configured tokens")

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 16))) as executor:
        futures = [
            executor.submit(fetch_with_correct_token, row)
            for _, row in accounts_df.iterrows()
        ]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                safe_error = str(exc)
                for token_client in token_clients.values():
                    safe_error = safe_error.replace(token_client.access_token, "[REDACTED]")
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
                    "live_daily_budget": 0.0,
                    "stopped_spend_today": 0.0,
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
                    "error": safe_error,
                    "budget_details": [],
                    "warnings": [],
                    "diagnostic_errors": [{
                        "entity_type": "snapshot",
                        "entity_id": "UNKNOWN",
                        "entity_name": "UNKNOWN",
                        "error": safe_error,
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
