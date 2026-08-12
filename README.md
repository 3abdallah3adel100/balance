# Meta Budget & Balance Monitor

Streamlit dashboard + headless GitHub Actions automation for Meta ad-account daily budgets, balance coverage, and WhatsApp recharge reports.

## Core rules

- Every **ACTIVE campaign** is included in Active Daily Budget even if it spent `0` today.
- **CBO:** campaign `daily_budget` is counted once.
- **ABO:** when the active campaign has no campaign daily budget, the app sums `daily_budget` for its **ACTIVE ad sets**.
- No CBO + ABO double counting.
- **Spend Today** is fetched independently, so spend already incurred today is retained even if a campaign is paused later.
- Agent reports use **Active Daily Budget only**.
- Critical alert: balance coverage is **1 day or less**.
- Recharge amount: `max(0, Active Daily Budget × 3 - Balance)`.
- Overall Allocation Budget is used **only** in the management Overall report.
- Automated runs explicitly use `Africa/Cairo` when deciding which calendar date is "today".

## WhatsApp routing

- `AA` → `201280871971`
- `HM` → `201015177863`
- `BM` → `201111901470`
- Overall management report → `201280871971`, `201098320008`

## Edit the Overall Allocation Budget

Open `config.py` and edit:

```python
OVERALL_ALLOCATION_BUDGET = 0.0
```

Example:

```python
OVERALL_ALLOCATION_BUDGET = 250000.0
```

This is used only by the Overall Management Report.

## Automation architecture

There is **no scheduled cron inside GitHub Actions**.

Automation path:

```text
cron-job.org
    ↓ HTTP POST
GitHub Actions workflow_dispatch
    ↓
python run_reports.py
    ↓
Meta API + calculations + WhatsApp reports
```

The workflow file is:

```text
.github/workflows/budget-report.yml
```

It runs only when triggered manually or through the GitHub Actions REST API.

Full cron-job.org configuration is in:

```text
CRONJOB_ORG_SETUP.md
```

## GitHub Repository Secrets

Create these under:

`Repository → Settings → Secrets and variables → Actions → Secrets`

Required secrets:

- `META_ACCESS_TOKEN`
- `WHATSAPP_ACCESS_TOKEN`
- `WHATSAPP_PHONE_NUMBER_ID`

Optional repository variables:

- `META_API_VERSION` = `v26.0`
- `WHATSAPP_API_VERSION` = `v26.0`

## Recommended first test

1. Push the project to the repository default branch.
2. Add the three required GitHub Repository Secrets.
3. Open **Actions → Meta Budget Report** and run it manually with `dry_run = true`.
4. Verify the report previews in the logs.
5. Configure cron-job.org using `CRONJOB_ORG_SETUP.md` with `dry_run = true` and run a test request.
6. Confirm cron-job.org successfully triggered the GitHub workflow.
7. Change cron-job.org request body to `dry_run = false` and test real WhatsApp delivery.
8. Enable the every-2-hours schedule in cron-job.org.

## Streamlit deployment

The same project still works as a normal Streamlit app:

1. Deploy `app.py` on Streamlit Community Cloud.
2. In Streamlit App Settings → Secrets, add your Streamlit secrets using `.streamlit/secrets.toml.example` as the template.
3. Use the dashboard buttons for manual refresh/preview/send.

The automatic run does not depend on Streamlit being awake. GitHub Actions runs `run_reports.py` directly.

## Important balance note

For prepaid accounts, verify that the balance source shown by the dashboard matches the usable Available Funds you expect. The project resolves coverage balance in this order:

1. `MANUAL_BALANCE_OVERRIDES` in `config.py`.
2. A clearly monetary `funding_source_details.display_string` if exposed.
3. Meta Ad Account `balance` field fallback.

The dashboard exposes `balance_source` for verification.

## Main files

- `app.py` — Streamlit UI/manual execution.
- `run_reports.py` — headless automation entry point used by GitHub Actions.
- `config.py` — recipients, buyer codes, allocation and thresholds.
- `meta_api.py` — Meta API and CBO/ABO budget logic.
- `reports.py` — calculations and WhatsApp message formatting.
- `whatsapp.py` — WhatsApp Cloud API sender.
- `.github/workflows/budget-report.yml` — API/manual-triggered GitHub Actions workflow; **no schedule**.
- `CRONJOB_ORG_SETUP.md` — exact cron-job.org trigger configuration.
- `requirements.txt` — Python dependencies.
