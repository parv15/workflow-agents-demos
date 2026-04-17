"""
Customer Escalation Agent: Intercom → Linear → Slack

Monitors Intercom conversations for frustration signals, checks customer value,
creates prioritized Linear issues, and alerts the right team in Slack.

Scalekit Agent Auth handles OAuth for all three connectors — token storage, refresh,
and API calls all go through a single interface. No manual token management.

Setup:
  cp .env.example .env        # fill in your credentials
  pip install -r requirements.txt
  python run_flow.py
"""
import os
import json
import time
import requests as http
from datetime import datetime, timezone
from dotenv import load_dotenv
import scalekit.client

load_dotenv()

# ── Scalekit client ───────────────────────────────────────────────────────────
sk = scalekit.client.ScalekitClient(
    client_id=os.environ["SCALEKIT_CLIENT_ID"],
    client_secret=os.environ["SCALEKIT_CLIENT_SECRET"],
    env_url=os.environ["SCALEKIT_ENV_URL"],
)
connect = sk.connect
actions = sk.actions

SLACK_CONNECTOR = os.environ.get("SLACK_CONNECTOR", "slack")
INTERCOM_CONNECTOR = os.environ.get("INTERCOM_CONNECTOR", "intercom")
LINEAR_CONNECTOR = os.environ.get("LINEAR_CONNECTOR", "linear")

CONNECTOR_USERS = {
    INTERCOM_CONNECTOR: os.environ["INTERCOM_USER"],
    LINEAR_CONNECTOR:   os.environ["LINEAR_USER"],
    SLACK_CONNECTOR:    os.environ["SLACK_USER"],
}

# Escalation config
MRR_THRESHOLD = float(os.environ.get("ESCALATION_MRR_THRESHOLD", 500))
MAX_OPEN_HOURS = float(os.environ.get("ESCALATION_MAX_OPEN_HOURS", 24))
MAX_REPLIES = int(os.environ.get("ESCALATION_MAX_REPLIES", 3))
SLACK_CHANNEL_ESCALATIONS = os.environ.get("SLACK_CHANNEL_ESCALATIONS", "#escalations")
SLACK_CHANNEL_TRIAGE = os.environ.get("SLACK_CHANNEL_TRIAGE", "#support-triage")
LINEAR_TEAM_ID = os.environ["LINEAR_TEAM_ID"]

# State file for de-duplication
STATE_DIR = os.path.join(os.path.dirname(__file__), "state")
STATE_FILE = os.path.join(STATE_DIR, "escalated_conversations.json")


# ── Auth helpers ──────────────────────────────────────────────────────────────
def ensure_authorized(connector: str) -> None:
    identifier = CONNECTOR_USERS[connector]
    resp = actions.get_or_create_connected_account(
        connection_name=connector, identifier=identifier
    )
    if resp.connected_account.status != "ACTIVE":
        link = actions.get_authorization_link(
            connection_name=connector, identifier=identifier
        ).link
        print(f"\n  [{connector}] Not authorized. Open:\n    {link}\n")
        input("  Press Enter after authorizing...")
    else:
        print(f"  {connector} ({identifier}) -- ACTIVE")


def get_intercom_token() -> str:
    """Get fresh Intercom OAuth token via Scalekit (auto-refreshes)."""
    resp = actions.get_connected_account(
        connection_name=INTERCOM_CONNECTOR,
        identifier=CONNECTOR_USERS[INTERCOM_CONNECTOR],
    )
    return resp.connected_account.authorization_details["oauth_token"]["access_token"]


def intercom_api(method: str, path: str, **kwargs) -> dict:
    """Call Intercom API directly using Scalekit-managed token."""
    token = get_intercom_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Intercom-Version": "2.11",
    }
    url = f"https://api.intercom.io{path}"
    r = getattr(http, method)(url, headers=headers, **kwargs)
    r.raise_for_status()
    return r.json()


def linear_tool(tool_name: str, **kwargs) -> dict:
    """Execute a Linear tool via Scalekit."""
    result = connect.execute_tool(
        tool_name=tool_name,
        identifier=CONNECTOR_USERS[LINEAR_CONNECTOR],
        tool_input=kwargs,
    )
    return result.data or {}


def slack_tool(tool_name: str, **kwargs) -> dict:
    """Execute a Slack tool via Scalekit."""
    result = connect.execute_tool(
        tool_name=tool_name,
        identifier=CONNECTOR_USERS[SLACK_CONNECTOR],
        tool_input=kwargs,
    )
    return result.data or {}


# ── State management ──────────────────────────────────────────────────────────
def _load_state() -> set:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return set(json.load(f))
    return set()


def _save_state(escalated: set) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(list(escalated)[-500:], f)


def _is_escalated(conv_id: str) -> bool:
    return conv_id in _load_state()


def _mark_escalated(conv_id: str) -> None:
    state = _load_state()
    state.add(conv_id)
    _save_state(state)


# ── Step 1: Fetch conversations ──────────────────────────────────────────────
def fetch_recent_conversations() -> list[dict]:
    """Fetch open Intercom conversations, filter out already-escalated ones."""
    data = intercom_api("get", "/conversations", params={"per_page": 20})
    conversations = data.get("conversations", [])
    return [c for c in conversations if not _is_escalated(str(c.get("id", "")))]


# ── Step 2: Detect escalation signals ────────────────────────────────────────
ESCALATION_KEYWORDS = [
    "cancel", "frustrated", "unacceptable", "terrible", "worst",
    "manager", "supervisor", "lawsuit", "legal", "refund",
    "ridiculous", "disappointed", "angry",
]


def extract_conversation_text(conversation: dict) -> str:
    """Extract text from conversation source and parts."""
    parts = []
    source = conversation.get("source") or {}
    if source.get("body"):
        parts.append(source["body"])
    if source.get("subject"):
        parts.append(source["subject"])
    # Get conversation parts if available
    conv_id = conversation.get("id")
    if conv_id:
        try:
            detail = intercom_api("get", f"/conversations/{conv_id}")
            for part in (detail.get("conversation_parts", {}).get("conversation_parts", [])):
                body = part.get("body", "")
                if body:
                    parts.append(body)
        except Exception:
            pass
    return " ".join(parts)


def detect_escalation_signals(conversation: dict) -> dict:
    """Combine keyword matching + heuristic rules to detect escalation."""
    signals = []
    score = 0.0

    text = extract_conversation_text(conversation).lower()

    # Keyword detection
    matched = [kw for kw in ESCALATION_KEYWORDS if kw in text]
    if matched:
        signals.append(f"Keywords: {', '.join(matched)}")
        score += 0.4

    # Open duration
    created_at = conversation.get("created_at")
    if created_at:
        created = datetime.fromtimestamp(created_at, tz=timezone.utc)
        hours_open = (datetime.now(timezone.utc) - created).total_seconds() / 3600
        if hours_open > MAX_OPEN_HOURS:
            signals.append(f"Open for {hours_open:.0f} hours")
            score += 0.3

    # Reply count
    stats = conversation.get("statistics") or {}
    reply_count = stats.get("count_replies", 0)
    if reply_count > MAX_REPLIES:
        signals.append(f"{reply_count} customer replies")
        score += 0.3

    # Determine sentiment based on keywords
    sentiment = "frustrated" if score >= 0.7 else "negative" if score >= 0.4 else "neutral"

    return {
        "should_escalate": score >= 0.4,
        "signals": signals,
        "score": score,
        "sentiment": sentiment,
        "escalation_reason": "; ".join(signals) if signals else "No escalation signals",
        "category": _guess_category(text),
    }


def _guess_category(text: str) -> str:
    if any(w in text for w in ["bug", "crash", "error", "broken", "not working"]):
        return "bug"
    if any(w in text for w in ["billing", "charge", "invoice", "payment", "refund"]):
        return "billing"
    if any(w in text for w in ["feature", "request", "wish", "would be nice"]):
        return "feature_request"
    return "account_issue"


# ── Step 3: Check customer value ─────────────────────────────────────────────
def get_customer_context(conversation: dict) -> dict:
    """Pull company data from Intercom to determine routing priority."""
    contacts = conversation.get("contacts", {}).get("contacts", [])
    if not contacts:
        return {"tier": "unknown", "mrr": 0, "company_name": "Unknown", "priority": "P1"}

    contact_id = contacts[0].get("id")
    if not contact_id:
        return {"tier": "unknown", "mrr": 0, "company_name": "Unknown", "priority": "P1"}

    try:
        contact = intercom_api("get", f"/contacts/{contact_id}")
        companies = (contact.get("companies") or {}).get("data", [])
        if not companies:
            return {"tier": "unknown", "mrr": 0, "company_name": "Unknown", "priority": "P1"}

        company_id = companies[0].get("id")
        company = intercom_api("get", f"/companies/{company_id}")

        plan = (company.get("plan") or {}).get("name", "unknown")
        mrr = company.get("monthly_spend", 0) or 0
        is_high_value = plan.lower() in ["enterprise", "business"] or mrr > MRR_THRESHOLD

        return {
            "tier": plan,
            "mrr": mrr,
            "company_name": company.get("name", "Unknown"),
            "priority": "P0" if is_high_value else "P1",
        }
    except Exception:
        return {"tier": "unknown", "mrr": 0, "company_name": "Unknown", "priority": "P1"}


# ── Step 4: Create Linear issue ──────────────────────────────────────────────
def create_linear_issue(conversation: dict, analysis: dict, customer: dict) -> dict:
    conv_id = conversation.get("id")
    title = (
        f"[Escalation] {customer.get('company_name', 'Unknown')} "
        f"— {analysis.get('category', 'support issue')}"
    )
    description = (
        f"## Customer Context\n"
        f"- **Company:** {customer.get('company_name', 'Unknown')}\n"
        f"- **Plan:** {customer['tier']}\n"
        f"- **MRR:** ${customer['mrr']}/mo\n\n"
        f"## Escalation Details\n"
        f"- **Priority:** {customer['priority']}\n"
        f"- **Sentiment:** {analysis.get('sentiment', 'unknown')}\n"
        f"- **Reason:** {analysis.get('escalation_reason', 'Multiple signals')}\n\n"
        f"## Links\n"
        f"- Intercom Conversation ID: {conv_id}\n"
    )

    try:
        result = linear_tool(
            "linear_issue_create",
            title=title,
            description=description,
            teamId=LINEAR_TEAM_ID,
        )
        issue_data = result.get("data", {}).get("issueCreate", {}).get("issue", {})
        return {
            "issue_id": issue_data.get("id", ""),
            "issue_url": issue_data.get("url", ""),
            "identifier": issue_data.get("identifier", ""),
        }
    except Exception as e:
        print(f"    Linear error: {e}")
        return {"issue_id": "", "issue_url": "", "identifier": "error"}


# ── Step 5: Alert Slack ──────────────────────────────────────────────────────
def send_slack_alert(conversation: dict, analysis: dict, customer: dict, linear_issue: dict) -> None:
    conv_id = conversation.get("id")
    is_high_value = customer["priority"] == "P0"

    channel = SLACK_CHANNEL_ESCALATIONS if is_high_value else SLACK_CHANNEL_TRIAGE

    emoji = ":rotating_light:" if is_high_value else ":warning:"
    message = (
        f"{emoji} *Customer Escalation — {customer['priority']}*\n\n"
        f"*Company:* {customer.get('company_name', 'Unknown')}\n"
        f"*Plan:* {customer['tier']} | *MRR:* ${customer['mrr']}/mo\n"
        f"*Sentiment:* {analysis.get('sentiment', 'unknown')}\n\n"
        f"*Reason:* {analysis.get('escalation_reason', 'Multiple signals')}\n"
        f"*Category:* {analysis.get('category', 'unknown')}\n\n"
        f"*Linear Issue:* {linear_issue.get('identifier', 'N/A')} — {linear_issue.get('issue_url', 'N/A')}\n"
        f"*Intercom:* Conversation #{conv_id}\n"
    )

    try:
        slack_tool("slack_send_message", channel=channel, text=message)
    except Exception:
        try:
            slack_tool("slack_send_message", channel=SLACK_CHANNEL_TRIAGE, text=message)
        except Exception as e:
            print(f"    Slack error: {e}")


# ── Step 6: Annotate Intercom conversation ───────────────────────────────────
def annotate_conversation(conversation: dict, customer: dict, linear_issue: dict, analysis: dict) -> None:
    conv_id = str(conversation.get("id"))
    note = (
        f"--- Escalation Agent ---\n"
        f"Priority: {customer['priority']}\n"
        f"Reason: {analysis.get('escalation_reason', 'Multiple signals')}\n"
        f"Sentiment: {analysis.get('sentiment', 'unknown')}\n"
        f"Linear Issue: {linear_issue.get('identifier', 'N/A')} — {linear_issue.get('issue_url', 'N/A')}\n"
        f"Routed to: {SLACK_CHANNEL_ESCALATIONS if customer['priority'] == 'P0' else SLACK_CHANNEL_TRIAGE}\n"
        f"---"
    )

    try:
        # Get admin ID for the note
        admins = intercom_api("get", "/admins")
        admin_id = admins.get("admins", [{}])[0].get("id")
        if admin_id:
            intercom_api("post", f"/conversations/{conv_id}/reply", json={
                "message_type": "note",
                "type": "admin",
                "admin_id": admin_id,
                "body": note,
            })
    except Exception as e:
        print(f"    Intercom note error: {e}")

    _mark_escalated(conv_id)


# ── Main pipeline ────────────────────────────────────────────────────────────
def run_pipeline():
    print("\n" + "=" * 60)
    print("Customer Escalation Agent: Intercom → Linear → Slack")
    print("=" * 60)

    # Step 0: Check auth
    print("\n-- Step 0: Checking connector auth --")
    for connector in CONNECTOR_USERS:
        ensure_authorized(connector)

    # Step 1: Fetch conversations
    print("\n-- Step 1: Fetching recent Intercom conversations --")
    conversations = fetch_recent_conversations()
    print(f"  Found {len(conversations)} conversation(s) to evaluate")

    if not conversations:
        print("\n  No new conversations to process.")
        print("\nFlow complete.")
        return

    escalated_count = 0
    start_time = time.time()

    for conv in conversations:
        conv_id = str(conv.get("id", ""))
        source = conv.get("source") or {}
        subject = source.get("subject") or source.get("body", "No subject") or "No subject"
        subject_short = str(subject)[:50]

        print(f"\n-- Conversation #{conv_id}: \"{subject_short}\" --")

        # Step 2: Detect signals
        analysis = detect_escalation_signals(conv)
        print(f"  Signals: {', '.join(analysis['signals']) or 'none'}")
        print(f"  Sentiment: {analysis['sentiment']} | Score: {analysis['score']:.2f}")

        if not analysis["should_escalate"]:
            print("  Skipped — no escalation signals")
            continue

        # Step 3: Customer value
        customer = get_customer_context(conv)
        print(f"  Customer: {customer['company_name']} | {customer['tier']} | ${customer['mrr']}/mo")
        print(f"  Priority: {customer['priority']}")

        # Step 4: Create Linear issue
        linear_issue = create_linear_issue(conv, analysis, customer)
        print(f"  Linear: {linear_issue.get('identifier', 'N/A')} created")

        # Step 5: Alert Slack
        send_slack_alert(conv, analysis, customer, linear_issue)
        channel = SLACK_CHANNEL_ESCALATIONS if customer["priority"] == "P0" else SLACK_CHANNEL_TRIAGE
        print(f"  Slack: {channel}")

        # Step 6: Annotate Intercom
        annotate_conversation(conv, customer, linear_issue, analysis)
        print(f"  Intercom: annotated")

        escalated_count += 1
        time.sleep(1)  # Rate limit buffer

    elapsed = time.time() - start_time
    print(f"\nFlow complete. Processed {len(conversations)} conversation(s), "
          f"escalated {escalated_count} in {elapsed:.0f} seconds.")


if __name__ == "__main__":
    polling = os.environ.get("POLLING_MODE", "false").lower() == "true"
    interval = int(os.environ.get("POLL_INTERVAL_MINUTES", 2))

    if polling:
        print(f"Polling mode enabled — running every {interval} minute(s). Ctrl+C to stop.\n")
        try:
            while True:
                try:
                    run_pipeline()
                except Exception as e:
                    print(f"\nPipeline error: {e}")
                print(f"\nNext run in {interval} minute(s)...")
                time.sleep(interval * 60)
        except KeyboardInterrupt:
            print("\nStopped by user.")
    else:
        run_pipeline()
