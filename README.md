# pk-clip-watcher

GitHub Actions watcher for new video posts from `@scubaryan_`, `@FearedBuck`, and `@yoxics`. It polls public Nitter RSS feeds on a schedule so the job no longer depends on a machine that sleeps. GitHub-hosted runners are free for this public repository.

## Secrets

Add both repository secrets under Settings → Secrets and variables → Actions. The workflow reads them from the environment and never prints them.

| Secret | Purpose |
| --- | --- |
| `WATCHER_WEBHOOK_URL` | Endpoint that receives `POST` JSON |
| `WATCHER_WEBHOOK_KEY` | Bearer token (`Authorization: Bearer <key>`) |

The body is `{"source":"watcher","new":n,"alert":bool,"clips":[...]}`. A clip has `handle`, `id`, `url`, `text`, `created_at`, and `found_at`.

The workflow POSTs when this run found clips, or when `health.json` has `alert: true` and `alert_sent` is still false for this outage. After a 2xx response it sets `alert_sent`. `watch.py` clears `alert_sent` when `alert` goes back to false. If either secret is empty, the run logs a warning and skips the POST without failing.

Clips are not queued across runs. A failed POST is not retried; an unsent health alert is.

## Run it

Actions → **watch** → **Run workflow**.

Locally, from the repo root (no secrets involved; this only polls and writes state):

```bash
python3 watch.py
```

Stdout is two lines, `NEW <n>` and `CLIPS <json>`. The process always exits 0. Warnings and errors also go to stderr. Fuller detail, including timestamps and which Nitter instance answered, is appended to `watch.log` (gitignored).

`state.json` stores the newest seen post id per account. `health.json` stores consecutive failure counts, `all_failed_streak`, `alert`, and `alert_sent`. Timestamps are left out of those files so a routine run does not create a commit. When a failure count, the alert, `alert_sent`, or a last id changes, the workflow commits the files as `github-actions[bot]` with message `chore: update watcher state [skip ci]` and pushes them to the branch it ran on (normally `main`). That branch has to allow the Actions bot to push. Those commits also keep GitHub from disabling the schedule after a long stretch with no repository activity.

## Schedule

UTC, matching US Eastern standard time (UTC−5), from 10:30 through 1:50:

- `30,40,50 15 * * *` (10:30, 10:40, 10:50)
- `*/10 0-6,16-23 * * *` (every 10 minutes, 11:00 through 1:50)

The crons are fixed to UTC−5, so during daylight time the window is an hour earlier in Eastern local time.
