#!/usr/bin/env python3
"""
setup_gateway.py - Step-by-step provisioning of the Hermes <-> AgentKit gateway

Run this once to:
  1. Create (or reuse) a connected account for GitHub, tied to HERMES_USER_IDENTIFIER.
  2. Create (or reuse) an MCP config that maps that connection to a narrow tool list.
  3. Ensure a per-user MCP instance exists (its own MCP server URL).
  4. Mint a session token Hermes can use to authenticate to that URL.
  5. Write a ready-to-paste mcp_servers block to hermes_mcp_config.snippet.yaml.

If the connected account isn't ACTIVE yet, this script prints an authorization
link, waits for you to approve it, and re-checks before minting the token —
so a single run either finishes cleanly or tells you exactly what's pending.

Usage:
    python setup_gateway.py
    python setup_gateway.py --identifier alice@example.com
"""

import argparse
import sys
import time

from settings import Settings
from sk_gateway import ScalekitGateway


def _print_step(n: int, total: int, label: str) -> None:
    print(f"\n[{n}/{total}] {label}")


def _wait_for_authorization(gateway: ScalekitGateway, identifier: str, link: str) -> str:
    print(f"      Open this link and approve access:\n\n      {link}\n")
    input("      Press Enter once you've authorized access... ")
    status, new_link = gateway.ensure_connected_account(identifier)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision the Hermes GitHub MCP gateway")
    parser.add_argument(
        "--identifier",
        default=Settings.HERMES_USER_IDENTIFIER,
        help="End-user identifier for the connected account (default: from .env)",
    )
    args = parser.parse_args()
    identifier = args.identifier

    print("Hermes GitHub Gateway — setup")
    print(f"  Scalekit env:  {Settings.SCALEKIT_ENV_URL}")
    print(f"  Connection:    {Settings.SCALEKIT_GITHUB_CONNECTION}")
    print(f"  Identifier:    {identifier}")
    print(f"  Tools exposed: {', '.join(Settings.MCP_TOOLS)}")

    gateway = ScalekitGateway()

    _print_step(1, 4, "Checking connected account...")
    status, auth_link = gateway.ensure_connected_account(identifier)
    print(f"      status = {status}")
    if status.lower() != "active":
        if not auth_link:
            print("      No authorization link returned and account isn't active — check your")
            print("      Scalekit dashboard for the connection's configuration.")
            return 1
        status = _wait_for_authorization(gateway, identifier, auth_link)
        if status.lower() != "active":
            print(f"      Still not active (status={status}). Re-run this script after authorizing.")
            return 1
    print("      ✓ connected account is ACTIVE")

    _print_step(2, 4, "Ensuring MCP config...")
    config_id, config_url = gateway.ensure_mcp_config()
    print(f"      config_id = {config_id}")

    _print_step(3, 4, "Ensuring per-user MCP instance...")
    instance_url = gateway.ensure_instance(config_id, identifier)
    print(f"      instance_url = {instance_url}")

    _print_step(4, 4, "Minting session token for Hermes...")
    token, expires_at = gateway.mint_session_token(config_id, identifier)
    print(f"      expires_at = {expires_at}")

    snippet = f"""mcp_servers:
  github_gateway:
    url: "{instance_url}"
    headers:
      Authorization: "Bearer {token}"
    tools:
      include: [{', '.join(Settings.MCP_TOOLS)}]
      prompts: false
      resources: false
"""

    with open(Settings.HERMES_CONFIG_SNIPPET_PATH, "w") as f:
        f.write(snippet)

    print(f"\nDone. Hermes config written to: {Settings.HERMES_CONFIG_SNIPPET_PATH}")
    print("\nNext steps:")
    print(f"  1. Paste the contents of {Settings.HERMES_CONFIG_SNIPPET_PATH} into ~/.hermes/config.yaml")
    print("  2. In a running Hermes session: /reload-mcp")
    print("  3. Verify: hermes mcp test github_gateway")
    print(f"  4. Token expires at {expires_at} — re-run this script to mint a fresh one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
