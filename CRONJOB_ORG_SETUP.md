# cron-job.org → GitHub Actions setup

This project does **not** use a GitHub cron schedule.

`cron-job.org` triggers the GitHub Actions workflow. GitHub then runs `python run_reports.py` on a GitHub-hosted runner.

## GitHub workflow trigger URL

Replace `OWNER` and `REPO` with your GitHub username/organization and repository name:

```text
https://api.github.com/repos/OWNER/REPO/actions/workflows/budget-report.yml/dispatches
```

## Fine-grained GitHub token

Create a fine-grained personal access token restricted to this repository only.

Repository permission required:

```text
Actions: Read and write
```

Do not put this token in the repository files.

## cron-job.org request

Create a cron job with:

### URL

```text
https://api.github.com/repos/OWNER/REPO/actions/workflows/budget-report.yml/dispatches
```

### Method

```text
POST
```

### Headers

```text
Accept: application/vnd.github+json
Authorization: Bearer YOUR_FINE_GRAINED_GITHUB_TOKEN
X-GitHub-Api-Version: 2026-03-10
Content-Type: application/json
```

### Request body

If your default branch is `main`:

```json
{
  "ref": "main",
  "inputs": {
    "dry_run": false
  }
}
```

Change `main` if your default branch has a different name.

## Schedule

Set the schedule in cron-job.org to every 2 hours.

Recommended first test:

1. First use `"dry_run": true` in the request body.
2. Execute the cron job manually with **Test run** / **Run now**.
3. Open GitHub → repository → Actions → Meta Budget Report.
4. Confirm the workflow ran and inspect the report previews.
5. Change the body to `"dry_run": false`.
6. Run it again and verify the WhatsApp messages.
7. Leave the recurring schedule enabled.

## GitHub repository secrets

These are still required in GitHub:

```text
META_ACCESS_TOKEN
WHATSAPP_ACCESS_TOKEN
WHATSAPP_PHONE_NUMBER_ID
```

Optional GitHub repository variables:

```text
META_API_VERSION=v26.0
WHATSAPP_API_VERSION=v26.0
```

The GitHub API trigger token used by cron-job.org is separate from these Meta/WhatsApp secrets.
