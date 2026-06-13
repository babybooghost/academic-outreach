# Deploying Academic Outreach

The app runs on **Vercel** (serverless Flask via `api/index.py`). This guide
covers automatic deploys and the environment variables you must set.

## How auto-deploy works

Vercel's GitHub integration deploys automatically: **every push to the
production branch (`main`) builds and ships** to `academic-outreach.vercel.app`.
Pull-request branches get preview deploys. Nothing else is required for
auto-deploy once the project is linked to this repo (it already is).

The GitHub Actions workflows are provided under **`deploy/github-workflows/`**
(they can't be pushed straight into `.github/workflows/` with a token that
lacks `workflow` scope). To enable them, copy both files into `.github/workflows/`
and push with a `workflow`-scoped token, or add them via the GitHub web UI:

- **`ci.yml`** — runs the test suite + the SMTP self-test on every push and PR.
  This is your quality gate; it needs no secrets.
- **`deploy.yml`** — *optional* test-gated deploy via the Vercel CLI. It stays
  inert unless you add a `VERCEL_TOKEN` secret. Use it only if you prefer
  CI-driven deploys over Vercel's native Git integration (don't enable both, or
  you'll deploy twice).

> Auto-deploy itself does **not** require these workflows — Vercel's native Git
> integration already deploys on push to `main`.

## Required environment variables (set in Vercel → Project → Settings → Environment Variables)

| Variable | Required? | What it does |
|---|---|---|
| `FLASK_SECRET_KEY` | **Yes (hosted)** | Signs sessions. Without it, logins silently reset on every cold start. Use a long random string. |
| `TURSO_DATABASE_URL` | **Strongly recommended** | Persistent cloud SQLite. Without it, data lives in `/tmp` and is wiped on every redeploy/cold start. |
| `TURSO_AUTH_TOKEN` | with Turso | Auth token for the Turso database. |
| `SIGNUP_INVITE_CODE` | recommended | Gates `/signup` so only people with the code can create a workspace (and spend your LLM key). |
| `ADMIN_KEY` | recommended | A fixed admin access key so you can always reach `/admin`. |
| `CRON_SECRET` | for auto-send | Protects `/api/cron/auto-send`; the daily cron must send `Authorization: Bearer <CRON_SECRET>`. |
| `LLM_PROVIDER` | for AI drafts | `openrouter`, `openai`, or `anthropic`. |
| `LLM_API_KEY` | for AI drafts | Your provider key. |
| `LLM_MODEL` | optional | Defaults to `google/gemini-2.5-flash-preview`. |
| `SHOW_BOOT_ERRORS` | no | Leave unset. Set to `1` only to temporarily expose startup tracebacks while debugging. |

Email delivery itself uses **each user's own SMTP credentials**, entered per
workspace in Setup — not a shared app mailbox.

## First-time setup checklist

1. **Create a Turso database** (free): `turso db create outreach`, then
   `turso db show outreach --url` and `turso db tokens create outreach`. Put the
   URL/token into Vercel env as `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN`.
2. **Set** `FLASK_SECRET_KEY`, `SIGNUP_INVITE_CODE`, `ADMIN_KEY`, `CRON_SECRET`,
   and the `LLM_*` vars in Vercel.
3. **Push to `main`** — Vercel auto-builds and deploys.
4. **Verify**: open `/health` — it should report a persistent (non-`/tmp`)
   storage mode once Turso is configured.
5. **Confirm email**: log in → Setup → enter your SMTP app password →
   "Send test email". Locally you can prove the send path end-to-end with
   `python scripts/smtp_selftest.py`.

## Optional: CI-driven deploy

If you want GitHub Actions to own deploys instead of Vercel's native build:

1. In Vercel, get the org and project IDs (`vercel link`, then read
   `.vercel/project.json`).
2. Add repo secrets: `VERCEL_TOKEN`, `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID`.
3. In Vercel → Git, turn off automatic production builds to avoid double deploys.

`deploy.yml` then runs the tests and deploys on every push to `main`.
