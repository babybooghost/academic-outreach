# Academic Outreach Email System

A Python application for generating personalized research outreach emails to professors. Designed for high school students seeking research opportunities.

This is **not** a mass-email tool. It prioritizes quality, authenticity, and human review. Every email is scored, checked for similarity, and requires manual approval before sending.

## Features

- **CSV Import** -- Load professor lists with research info
- **Web Scraping Enrichment** -- Auto-extract research interests from faculty pages
- **AI Summarization** -- Optional LLM-powered research summaries (OpenRouter, OpenAI, Anthropic)
- **Personalized Email Generation** -- 4 template variants with controlled variation
- **Quality Scoring (1-10)** -- Specificity, authenticity, relevance, conciseness, completeness
- **Similarity Detection** -- ML-based cross-draft comparison flags repetitive emails
- **Genericness Detector** -- Warns if an email could apply to almost anyone
- **Follow-up Generator** -- Softer template for 7-10 day follow-ups
- **Interactive Review** -- CLI and web UI for approve/reject/edit workflow
- **Gmail Draft Creation** -- Creates drafts in Gmail (default, safest mode)
- **SMTP Sending** -- Supports Gmail, Outlook, and Hotmail
- **Suppression List** -- Never email the same person twice
- **Rate Limiting** -- Configurable send limits and cooldowns
- **Full Audit Trail** -- Every action logged to SQLite + log files
- **Multi-user** -- Multiple sender profiles for you and friends
- **Export** -- CSV, JSON, TXT, and tracking spreadsheet exports

## Hosted Storage Status

The current hosted web app still has one important deployment limitation:

- On Vercel without a real database service, the app stores workspace files under `/tmp`, which is temporary instance storage.
- That means user workspaces are isolated from each other, but hosted data can disappear after deploys, cold starts, or instance replacement.
- The app now reports this clearly in the UI banner and `/health` output so the deployment mode is visible.

Important caveat:

- The existing Turso/libsql adapter is not yet tenant-safe for the web workspace model, because the hosted app currently isolates users with per-workspace database files.
- A proper remote multi-tenant schema migration is still needed before hosted persistent multi-user storage is trustworthy.

## Quick Start

### 1. Install Python

Download Python 3.10+ from [python.org](https://www.python.org/downloads/). During install, **check "Add Python to PATH"**.

### 2. Set Up the Project

Open a terminal (Command Prompt or PowerShell on Windows) and run:

```bash
# Navigate to the project folder
cd "path/to/New Email"

# Create a virtual environment
python -m venv venv

# Activate it
# Windows:
venv\Scripts\activate
# Mac/Linux:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configure Environment

```bash
# Copy the example env file
cp .env.example .env
```

Edit `.env` with your settings. At minimum, set:
- `LLM_PROVIDER=openrouter` and `LLM_API_KEY` (for AI-powered summaries)
- `SENDER_EMAIL` (your email address)
- `EMAIL_PROVIDER` (`gmail`, `outlook`, or `hotmail`)

For Gmail draft creation, you'll also need `credentials.json` from Google Cloud Console (see `.env.example` for instructions).

### 4. Create Your Profile

```bash
python main.py profile --add
```

This prompts you for your name, school, grade, email, interests, and background. These details are used honestly in the generated emails.

### 5. Import Professors

Prepare a CSV file (see [CSV Schema](#csv-schema) below) or use the sample:

```bash
python main.py import data/sample_professors.csv
```

### 6. Enrich Professors (Optional)

Scrape faculty pages to extract research info:

```bash
python main.py enrich --limit 5
```

### 7. Generate Emails

```bash
python main.py generate --all
```

This runs the full pipeline: summarize -> personalize -> render emails -> score -> check similarity. You'll see a table with scores and warnings.

### 8. Review Emails

**CLI Review:**
```bash
python main.py review
```

**Web UI Review:**
```bash
python main.py web
# Open http://localhost:5000 in your browser
```

For each email you can: **approve**, **reject**, **edit**, or **regenerate**.

### 9. Send (Draft Mode)

```bash
# Create Gmail drafts (safest - you can review in Gmail first)
python main.py send --draft-only

# Dry run (just logs, doesn't touch Gmail)
python main.py send --dry-run

# Actually send approved emails
python main.py send --execute --method smtp
```

### 10. Export

```bash
python main.py export --format csv        # All drafts as CSV
python main.py export --format tracking   # Tracking spreadsheet
python main.py export --format json       # Full JSON export
python main.py export --format txt        # Individual .txt files
```

## CSV Schema

Your professor CSV should have these columns (all optional except `name` and `email`):

| Column | Required | Description |
|--------|----------|-------------|
| `name` | Yes | Professor's full name |
| `email` | Yes | Email address |
| `university` | No | University name |
| `department` | No | Department name |
| `field` | No | Research field (e.g., "Machine Learning") |
| `title` | No | Academic title (e.g., "Assistant Professor") |
| `lab_name` | No | Lab or research group name |
| `profile_url` | No | Faculty page URL (used for enrichment) |
| `research_summary` | No | Brief description of their research |
| `recent_work` | No | Recent papers or projects |
| `notes` | No | Your personal notes |

## CLI Commands

| Command | Description |
|---------|-------------|
| `python main.py import <csv>` | Import professors from CSV |
| `python main.py enrich [--limit N]` | Scrape faculty pages for research info |
| `python main.py generate --all` | Generate personalized emails |
| `python main.py review` | Interactive review in terminal |
| `python main.py approve 1 2 3` | Approve specific draft IDs |
| `python main.py send --dry-run` | Preview what would be sent |
| `python main.py send --draft-only` | Create Gmail drafts |
| `python main.py send --execute` | Send approved emails |
| `python main.py followup` | Generate follow-up emails |
| `python main.py export --format csv` | Export drafts |
| `python main.py status` | Show dashboard |
| `python main.py suppress email@x.com` | Add to do-not-contact list |
| `python main.py profile --add` | Add a sender profile |
| `python main.py profile --list` | List sender profiles |
| `python main.py model --list` | List available LLM models |
| `python main.py model --set gemini-pro` | Change LLM model |
| `python main.py web` | Launch web review UI |

## LLM Models

Available via OpenRouter (set in `.env` or via `python main.py model --set`):

| Alias | Model | Best For |
|-------|-------|----------|
| `gemini-flash` | Gemini 2.5 Flash | Fast, cheap, good quality |
| `gemini-pro` | Gemini 2.5 Pro | Best quality/cost balance |
| `claude-haiku` | Claude Haiku 4.5 | Fast, affordable |
| `claude-sonnet` | Claude Sonnet 4.6 | Strong quality |
| `claude-opus` | Claude Opus 4.6 | Highest quality |

## Email Providers

| Provider | SMTP Host | Notes |
|----------|-----------|-------|
| Gmail | smtp.gmail.com | Use App Password, not regular password |
| Outlook | smtp-mail.outlook.com | Regular password or app password |
| Hotmail | smtp-mail.outlook.com | Same as Outlook |

Set `EMAIL_PROVIDER=outlook` in `.env` for Outlook/Hotmail.

## Scoring System

Each email is scored 1-10 across 5 dimensions:

| Dimension | Weight | What It Measures |
|-----------|--------|-----------------|
| Specificity | 30% | References to professor's actual research |
| Authenticity | 20% | Unique content vs template boilerplate |
| Relevance | 20% | Match between your interests and their work |
| Conciseness | 15% | Appropriate length (150-350 words) |
| Completeness | 15% | Has all required sections |

**Warnings** flag issues like: "No concrete research reference", "Could apply to almost anyone", "Too similar to other drafts", "Research hook is weak".

## Safety Features

- **Human review required** -- No email sends without explicit approval
- **Draft mode default** -- Creates Gmail drafts first, doesn't auto-send
- **Rate limiting** -- Max 15 emails/hour, 30 per session (configurable)
- **Cooldowns** -- 30-90 second random delay between sends
- **Suppression list** -- Prevents duplicate contact
- **Dry-run mode** -- Test everything without sending
- **Audit trail** -- Every action logged with timestamps

## Workflow Best Practices

1. Research 10-15 professors per batch
2. Use enrichment to pull real research data
3. Generate emails and review scores
4. Fix any flagged emails (low scores, high similarity)
5. Send in small batches (5-10 at a time)
6. Wait 7-10 days before following up
7. Only send ONE follow-up per professor
8. Track replies and close the loop

## Targeting Tips

- **Best targets**: Assistant/Associate Professors with computational research
- **Good signals**: Faculty pages mentioning "student researchers", "REU", "mentoring"
- **Aim for**: 40% assistant, 35% associate, 25% selected full professors
- **Focus on**: Fields where you can honestly contribute (coding, data analysis, literature review)

## Troubleshooting

**"ModuleNotFoundError"** -- Make sure your virtual environment is activated: `venv\Scripts\activate`

**Gmail API errors** -- Ensure `credentials.json` is in the project root. Delete `token.json` to re-authenticate.

**Outlook SMTP errors** -- Enable "Allow less secure apps" or use an app password.

**"ConfigError: Scoring weights must sum to 1.0"** -- Check your `config.yaml` scoring weights.

**Emails flagged as generic** -- Add more detail to your CSV's `research_summary` column or run enrichment.

**Low scores** -- Usually means insufficient data about the professor. Try enrichment or add manual notes.

## Project Structure

```
New Email/
  app/
    config.py           # Configuration from .env + config.yaml
    models.py           # Data models (Professor, Draft, etc.)
    database.py         # SQLite database layer
    csv_loader.py       # CSV import
    enricher.py         # Web scraping
    summarizer.py       # Research summarization
    personalizer.py     # Talking point generation
    template_engine.py  # Email rendering
    scorer.py           # Quality scoring
    similarity.py       # Cross-draft similarity
    reviewer.py         # Review workflow
    sender.py           # Gmail API + SMTP sending
    storage.py          # Export functionality
    logger.py           # Logging
    cli.py              # CLI commands
    web/                # Flask web UI
    templates/emails/   # Jinja2 email templates
  data/                 # CSV files + SQLite database
  outputs/              # Generated exports
  logs/                 # Audit logs
  .env                  # Your secrets (not committed)
  config.yaml           # App settings (auto-generated)
  main.py               # Entry point
```
