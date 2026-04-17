# Customer Escalation Agent

Monitors Intercom conversations for frustration signals, checks customer value, creates prioritized Linear issues, alerts the right Slack channel, and writes an internal note back to Intercom — all through a single [Scalekit Agent Auth](https://scalekit.com) interface.

Built so that no OAuth, token storage, or refresh logic lives in this repo. Linear and Slack use `connect.execute_tool()`; Intercom uses Scalekit-managed OAuth tokens with direct REST calls.

```
Intercom conversations  →  Signal detection (keywords + heuristics)
                        →  Customer value check (plan / MRR)
                        →  Linear issue (P0 or P1)
                        →  Slack alert (#customer-support or #support-engineering)
                        →  Intercom internal note (with Linear link)
```

A companion blog post walks through the architecture end-to-end: [How to Build a Customer Escalation Agent with Intercom, Linear, and Slack](./How%20to%20Build%20a%20Customer%20Escalation%20Agent%20with%20Intercom%2C%20Linear%2C%20and%20Slack.md).

## Prerequisites

- [Scalekit account](https://scalekit.com) — free tier works
- Intercom workspace with API access (conversation + company data scopes)
- Linear workspace with at least one team
- Slack workspace with two routing channels (e.g. `#customer-support`, `#support-engineering`)
- Python 3.11+

## Setup

### 1. Set up Scalekit connectors

In **app.scalekit.com → Agent Auth → Connections**, create three connectors:

| Connection | Required scopes |
|---|---|
| Intercom | Conversation read, company data |
| Linear | Issue create, issue update |
| Slack | `chat:write`, `chat:write.public`, `users:read` |

Copy each connection name from the dashboard (Scalekit may append a suffix, e.g. `slack-sKfekCVz`). You'll paste these into `.env`.

> **Slack note:** With `chat:write.public`, the bot posts to public channels without being invited. For **private** channels, you must `/invite @your-bot-name` first.

### 2. Configure environment

```bash
cp .env.example .env
```

Fill in `.env`:

- `SCALEKIT_*` — from **Settings → API Credentials**
- `INTERCOM_USER` / `LINEAR_USER` / `SLACK_USER` — the same email for all three; this is the Scalekit identifier for this user across connectors
- `*_CONNECTOR` — exact connection names from the dashboard
- `LINEAR_TEAM_ID` — Linear → **Settings → Workspace → API** → copy the Team ID
- `SLACK_CHANNEL_*` — the two channels you route to

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Run

```bash
python run_flow.py
```

The first run checks auth for each connector. If any are not yet authorized, a magic link is printed — open it in a browser, complete OAuth, press Enter. Every subsequent run goes straight through.

## How it works

```
Step 0 — Auth check
  Scalekit verifies all three connectors are ACTIVE.
  Magic link printed for any that need authorization.

Step 1 — Fetch conversations
  Intercom REST API (via Scalekit-managed token).
  Already-escalated IDs are filtered out using state/escalated_conversations.json.

Step 2 — Detect escalation signals
  Keyword match + heuristic rules. Cumulative score ≥ 0.4 triggers escalation.
    keywords ............... +0.4   (cancel, frustrated, manager, refund, ...)
    open > 24h ............. +0.3
    replies > 3 ............ +0.3

Step 3 — Customer value check
  Intercom REST API: contact → company → plan/MRR.
  Enterprise OR MRR > $500 → P0, otherwise P1.

Step 4 — Create Linear issue
  connect.execute_tool("linear_issue_create")
  Title, description, and teamId.

Step 5 — Alert Slack
  connect.execute_tool("slack_send_message")
  P0 → SLACK_CHANNEL_ESCALATIONS, P1 → SLACK_CHANNEL_TRIAGE.
  Falls back to SLACK_CHANNEL_TRIAGE if the target channel rejects the post.

Step 6 — Annotate Intercom
  Intercom REST API: POST /conversations/{id}/reply as an admin internal note.
  Conversation ID added to state file to prevent duplicate escalations.
```

## Integration patterns

This agent uses two Scalekit integration patterns side-by-side:

| Service | Pattern | How it's called |
|---|---|---|
| Linear | Pre-built tool | `connect.execute_tool("linear_issue_create", ...)` |
| Slack | Pre-built tool | `connect.execute_tool("slack_send_message", ...)` |
| Intercom | Direct API + managed token | `requests.get(..., Bearer <token-from-scalekit>)` |

Both patterns go through Scalekit for token storage and refresh — there is no token management code in this repo.

## Running modes

### Single run (default)

`POLLING_MODE=false` — runs the pipeline once and exits. Ideal for cron.

```cron
*/2 9-18 * * 1-5 cd /path/to/intercom-linear-slack-agent && python run_flow.py >> logs/run.log 2>&1
```

### Continuous polling

`POLLING_MODE=true` — loops every `POLL_INTERVAL_MINUTES`. Per-cycle errors are caught and logged; the loop keeps running. Stop with `Ctrl+C`.

## Tuning

All thresholds are environment variables, no code changes needed:

| Variable | Default | Effect |
|---|---|---|
| `ESCALATION_MRR_THRESHOLD` | `500` | MRR (USD/mo) that qualifies a customer as P0 |
| `ESCALATION_MAX_OPEN_HOURS` | `24` | Age in hours that contributes +0.3 to the score |
| `ESCALATION_MAX_REPLIES` | `3` | Reply count above which +0.3 is added |

To extend the keyword set, edit `ESCALATION_KEYWORDS` in [run_flow.py](./run_flow.py).

## Project structure

```
├── run_flow.py          # main pipeline — all logic in one file
├── .env.example         # environment template
├── requirements.txt
├── .gitignore
└── README.md
```

State (`state/escalated_conversations.json`) is created automatically on first run and gitignored.

## Notes

- **Intercom:** Without company-data scopes, customer context lookups silently fail and all escalations default to P1. Re-authorize with the correct scopes if you see every escalation routing to `#support-engineering`.
- **Linear:** The tool name is `linear_issue_create` (not `linear_create_issue`), and the team parameter is `teamId` (camelCase). Passing `priority` as an integer currently triggers a template error — this integration omits it.
- **Slack:** Token refresh is handled automatically by Scalekit. If you see `not_in_channel` for a private channel, invite the bot with `/invite @your-bot-name`.
- **De-duplication:** Each escalated conversation ID is appended to `state/escalated_conversations.json`. To re-evaluate a conversation (e.g. after it's resolved and reopened), remove its ID from that file.
