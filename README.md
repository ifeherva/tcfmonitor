# TCF exam availability monitor

This project checks several TCF Canada exam tables periodically. It emails a configurable list of recipients when an exam changes
from unavailable to available, or when a newly listed exam is already available.
It does not repeat an alert while the same session stays open.

The monitor is deliberately read-only: it links to the booking page but does not
attempt to reserve or purchase an exam.

## GitHub setup

1. Push this repository to GitHub and open **Settings → Secrets and variables →
   Actions**.
2. Add these **repository variables**:

   | Variable | Example | Notes |
   | --- | --- | --- |
   | `SMTP_HOST` | `smtp.gmail.com` | Your mail provider's SMTP server |
   | `SMTP_PORT` | `587` | Use `465` for implicit TLS |
   | `SMTP_STARTTLS` | `true` | Normally `true` with port 587 |
   | `SMTP_USE_SSL` | `false` | Normally `true` only with port 465 |

3. Add these **repository secrets**:

   | Secret | Notes |
   | --- | --- |
   | `SMTP_USERNAME` | SMTP login; often the complete sender email address |
   | `SMTP_PASSWORD` | SMTP password or provider-specific app password |
   | `EMAIL_FROM` | Sender address; optional if it is the same as `SMTP_USERNAME` |
   | `NOTIFY_EMAILS` | Comma-, semicolon-, or newline-separated recipient list |

   For Gmail, enable two-step verification and create an app password. Use the
   Gmail address as `SMTP_USERNAME` and the 16-character app password as
   `SMTP_PASSWORD`; regular account passwords are not accepted.

4. Open **Actions → Monitor TCF exam availability → Run workflow**, select
   **Send a test email before checking the exam page**, and run it. Confirm that
   the workflow succeeds and every configured recipient receives the test. The
   scheduled workflow then runs every five minutes without sending test messages.

GitHub schedules are best-effort and can run a few minutes late, especially near
the start of an hour. GitHub may disable schedules in a public repository after
60 days without repository activity; re-enable the workflow if that happens.

> **Actions cost:** a five-minute schedule starts about 8,640 jobs in a 30-day
> month. GitHub rounds each private-repository job up to a full billable minute,
> so this can exceed the included monthly allowance even though each check is
> short. Standard runners are free for public repositories. Review
> [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
> before enabling the schedule in a private repository.

## How change detection works

The workflow stores the last observed session statuses in a small GitHub Actions
cache. Each successful check compares the live page with that snapshot:

- full/closed → available: email;
- a new session first appears as available: email;
- available → still available: no duplicate email;
- available → full → available: email again.

If email delivery fails, the new state is not saved, so the next run retries the
notification. A missing table or malformed page fails safely instead of treating
the page as newly available.

## Add another page

Add another object to [`config/monitors.json`](config/monitors.json). Pages using
the same Oncord exam table need only an `id`, `name`, `url`, and the existing
`oncord_exam_table` parser. A site with different markup will need a new parser in
`tcf_monitor/monitor.py` and a matching fixture test.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -v
python -m tcf_monitor --dry-run
```

`--dry-run` fetches and parses all configured pages, prints any currently open
sessions, and does not email or modify saved state.

To exercise the real SMTP configuration locally, export the variables described
above and run `python -m tcf_monitor --test-email --dry-run`. It sends one test
message, then checks the live page without changing monitor state.
