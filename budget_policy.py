"""Current live daily budgets plus today's spend of stopped budget owners."""
def closing_budgets(campaigns, adsets, ads, campaign_spend, adset_spend, money):
    campaigns = {str(x['id']): x for x in campaigns}
    active_campaigns, active_sets = set(), set()
    for ad in ads:
        if ad.get('effective_status') == 'ACTIVE':
            active_campaigns.add(str(ad.get('campaign_id') or ''))
            active_sets.add(str(ad.get('adset_id') or ''))
    owners = {}
    for cid, campaign in campaigns.items():
        daily = money(campaign.get('daily_budget')) or 0
        lifetime = money(campaign.get('lifetime_budget')) or 0
        if daily <= 0 and lifetime <= 0:
            continue
        live = campaign.get('effective_status') == 'ACTIVE' and cid in active_campaigns
        amount = daily if live else float(campaign_spend.get(cid, 0) or 0)
        if amount > 0:
            owners[('CBO', cid)] = dict(amount=amount, mode='live' if live else 'spent', label=campaign.get('name') or cid)
    for adset in adsets:
        sid, cid = str(adset['id']), str(adset.get('campaign_id') or '')
        campaign = campaigns.get(cid)
        if campaign is None and sid not in active_sets and float(adset_spend.get(sid, 0) or 0) == 0:
            continue  # Unused historical orphan cannot contribute to today's closing total.
        if campaign is None:
            raise ValueError('Missing campaign metadata for budget calculation: ' + cid)
        if (money(campaign.get('daily_budget')) or 0) > 0 or (money(campaign.get('lifetime_budget')) or 0) > 0:
            continue  # Campaign owns this budget: never add its child budgets/spend twice.
        live = (campaign.get('effective_status') == 'ACTIVE' and
                adset.get('effective_status') == 'ACTIVE' and sid in active_sets)
        daily = money(adset.get('daily_budget')) or 0
        amount = daily if live else float(adset_spend.get(sid, 0) or 0)
        if amount > 0:
            owners[('ABO', sid)] = dict(amount=amount, mode='live' if live else 'spent', label=adset.get('name') or sid)
    return owners


def allocation_budget_breakdown(campaigns, adsets, ads, campaign_spend,
                                adset_spend, money, ad_spend_rows=None):
    """Compute the two Allocation totals without mixing budgets and spent money.

    The original (working) Allocation budget rule was ACTIVE + positive spend
    today. Add the missing third-level guard: at least one effective ACTIVE ad.

    A daily-budget *owner* is a CBO campaign or an ABO adset, never both.
    Budget figures are taken once per owner. Stopped spend comes from unique
    ad-level Insights rows; a stopped ad may be inside a still-running owner.
    A stopped ad's spend is added only when its budget owner is not live.
    A live ABO ad set already contains spend of paused ads inside that ad set.
    Likewise, a live CBO campaign already contains spend of paused ads/ad sets
    inside that campaign. Only stopped budget owners contribute spent money.

    `ad_spend_rows=None` exists only for old clients/tests without ad insights:
    in that compatibility path only entirely stopped owners can be reconciled.
    """
    campaigns = {str(x['id']): x for x in campaigns}
    adsets = {str(x['id']): x for x in adsets}
    ads = {str(x['id']): x for x in ads if x.get('id')}

    def active(entity):
        return str((entity or {}).get('effective_status') or '').upper() == 'ACTIVE'

    def amount(value):
        number = float(value or 0)
        return number if number > 0 else 0.0

    def parent_owns_budget(campaign):
        return (money(campaign.get('daily_budget')) or 0) > 0 or (money(campaign.get('lifetime_budget')) or 0) > 0

    # Match only ad IDs that belong to known parents; a paused ancestor is not live.
    live_campaigns, live_adsets = set(), set()
    for ad in ads.values():
        sid, cid = str(ad.get('adset_id') or ''), str(ad.get('campaign_id') or '')
        parent, group = campaigns.get(cid), adsets.get(sid)
        if not (active(ad) and active(parent) and active(group)):
            continue
        if str(group.get('campaign_id') or '') != cid:
            raise ValueError(f'Inconsistent ad / adset parent: {sid}, {cid}')
        live_campaigns.add(cid)
        live_adsets.add(sid)

    details = []
    counted_budget_ids = set()
    for cid, campaign in campaigns.items():
        daily = money(campaign.get('daily_budget')) or 0
        # The old working code required spend > 0, unlike the broken new policy.
        if daily > 0 and cid in live_campaigns and amount(campaign_spend.get(cid)) > 0:
            key = ('CBO', cid)
            if key not in counted_budget_ids:
                details.append(dict(budget_level='CBO', entity_id=cid,
                                    daily_budget=float(daily), calculation_mode='live',
                                    entity_name=campaign.get('name') or cid))
                counted_budget_ids.add(key)

    for sid, group in adsets.items():
        cid = str(group.get('campaign_id') or '')
        campaign = campaigns.get(cid)
        if campaign is None:
            if sid in live_adsets or amount(adset_spend.get(sid)):
                raise ValueError(f'Missing campaign metadata: {cid} (adset {sid})')
            continue
        if parent_owns_budget(campaign):
            continue  # Never add ABO underneath CBO or a lifetime-funded CBO.
        daily = money(group.get('daily_budget')) or 0
        if daily > 0 and sid in live_adsets and amount(adset_spend.get(sid)) > 0:
            key = ('ABO', sid)
            if key not in counted_budget_ids:
                details.append(dict(budget_level='ABO', entity_id=sid,
                                    daily_budget=float(daily), calculation_mode='live',
                                    entity_name=group.get('name') or sid))
                counted_budget_ids.add(key)

    if ad_spend_rows is not None:
        # Exactly one Insights row per ad/day. Disallow silent double counting.
        seen_ads = set()
        for metric in ad_spend_rows:
            spent = amount(metric.get('spend'))
            if not spent:
                continue
            aid = str(metric.get('ad_id') or '')
            cid = str(metric.get('campaign_id') or '')
            sid = str(metric.get('adset_id') or '')
            if not aid or not cid or not sid:
                raise ValueError('Missing ad/campaign/adset ID in positive-spend Insights')
            if aid in seen_ads:
                raise ValueError(f'Duplicate ad_id in daily Insights: {aid}')
            seen_ads.add(aid)
            campaign, group, ad = campaigns.get(cid), adsets.get(sid), ads.get(aid)
            if campaign is None or group is None:
                raise ValueError(f'Missing parent metadata for spent ad {aid}: {cid}/{sid}')
            if str(group.get('campaign_id') or '') != cid:
                raise ValueError(f'Ad set/campaign mismatch for spent ad {aid}')
            if ad and (str(ad.get('campaign_id') or '') != cid or str(ad.get('adset_id') or '') != sid):
                raise ValueError(f'Ad metadata/Insights mismatch for spent ad {aid}')
            # The account edge may omit archived ads: if its parents are still
            # active, status is ambiguous and we must NOT guess it was stopped.
            if ad is None and active(campaign) and active(group):
                raise ValueError(f'Missing status for spent ad {aid} under active parents')
            level = 'CBO' if parent_owns_budget(campaign) else 'ABO'
            owner_id = cid if level == 'CBO' else sid
            # Do not add spend from stopped ads inside a *live* budget owner:
            # its daily budget already covers the entire owner, including any
            # paused ads/adsets that consumed some of the budget today.
            if (level, owner_id) in counted_budget_ids:
                continue
            # Active delivery under a non-daily (e.g. lifetime-funded) owner
            # is not stopped spend either.
            if ad and active(ad) and active(campaign) and active(group):
                continue
            details.append(dict(budget_level=level, entity_id=aid,
                                daily_budget=spent, calculation_mode='spent',
                                entity_name=f'Stopped ad {aid}',
                                campaign_id=cid, adset_id=sid))
    else:
        # Compatibility for fake legacy clients without per-ad Insights.
        # Only entirely inactive budget owners need stopped spend.
        for cid, campaign in campaigns.items():
            if not parent_owns_budget(campaign) or ('CBO', cid) in counted_budget_ids:
                continue
            spent = amount(campaign_spend.get(cid))
            if spent:
                details.append(dict(budget_level='CBO', entity_id=cid,
                                    daily_budget=spent, calculation_mode='spent',
                                    entity_name=campaign.get('name') or cid))
        for sid, group in adsets.items():
            cid = str(group.get('campaign_id') or '')
            campaign = campaigns.get(cid)
            if campaign is None:
                if amount(adset_spend.get(sid)):
                    raise ValueError(f'Missing campaign metadata for spent adset: {sid}')
                continue
            if parent_owns_budget(campaign) or ('ABO', sid) in counted_budget_ids:
                continue
            spent = amount(adset_spend.get(sid))
            if spent:
                details.append(dict(budget_level='ABO', entity_id=sid,
                                    daily_budget=spent, calculation_mode='spent',
                                    entity_name=group.get('name') or sid))

    live_total = sum(x['daily_budget'] for x in details if x['calculation_mode'] == 'live')
    stopped_total = sum(x['daily_budget'] for x in details if x['calculation_mode'] == 'spent')
    return live_total, stopped_total, details
