"""
settings.py - Configuration Management for Hermes GitHub Gateway Agent

Centralizes environment configuration for the setup script that provisions
a Scalekit AgentKit MCP gateway and points Hermes Agent's config.yaml at it.

Uses python-dotenv to load from .env; raises where a value is required and
missing rather than silently defaulting to something unsafe.
"""

import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


class Settings:
    """
    Application settings loaded from environment variables.

    Configuration Priority:
    1. Environment variables
    2. .env file
    3. Default values (only where a safe default exists)
    """

    # ============================================================================
    # SCALEKIT CONFIGURATION
    # ============================================================================

    # Scalekit environment URL (e.g., https://your-env.scalekit.com)
    SCALEKIT_ENV_URL: str = os.getenv("SCALEKIT_ENV_URL", "")

    # Scalekit OAuth client credentials (from your Scalekit dashboard)
    SCALEKIT_CLIENT_ID: str = os.getenv("SCALEKIT_CLIENT_ID", "")
    SCALEKIT_CLIENT_SECRET: str = os.getenv("SCALEKIT_CLIENT_SECRET", "")

    # Name of the GitHub connection configured in the Scalekit dashboard.
    # This must match a connection you created under Connections > GitHub.
    SCALEKIT_GITHUB_CONNECTION: str = os.getenv("SCALEKIT_GITHUB_CONNECTION", "github")

    # ============================================================================
    # MCP GATEWAY CONFIGURATION
    # ============================================================================

    # Human-readable name for the MCP config this script creates/reuses.
    # Scalekit uses this to find an existing config on repeat runs (idempotent).
    MCP_CONFIG_NAME: str = os.getenv("MCP_CONFIG_NAME", "hermes-github-gateway")

    # Tools exposed to Hermes through this gateway. Keep this list narrow —
    # Hermes only ever sees these tool names, nothing else the connector exposes.
    MCP_TOOLS = [
        t.strip()
        for t in os.getenv(
            "MCP_TOOLS", "github_search_issues,github_issue_labels_add,github_issue_get"
        ).split(",")
        if t.strip()
    ]

    # ============================================================================
    # END USER / HERMES IDENTITY
    # ============================================================================

    # The identifier Hermes will act as. In production this is normally the
    # authenticated user's email or an opaque user ID from your own system —
    # NOT a shared/global constant. For a single-operator local setup, a
    # stable string like "local-hermes-user" is fine.
    HERMES_USER_IDENTIFIER: str = os.getenv("HERMES_USER_IDENTIFIER", "local-hermes-user")

    # How long the minted MCP session token stays valid, in hours.
    SESSION_TOKEN_TTL_HOURS: int = int(os.getenv("SESSION_TOKEN_TTL_HOURS", "8"))

    # ============================================================================
    # OUTPUT
    # ============================================================================

    # Where to write the generated Hermes config snippet. Defaults to a local
    # file in this folder so nothing is silently written into ~/.hermes/.
    HERMES_CONFIG_SNIPPET_PATH: str = os.getenv(
        "HERMES_CONFIG_SNIPPET_PATH", "hermes_mcp_config.snippet.yaml"
    )

    @classmethod
    def validate(cls) -> None:
        """Raise a clear error for anything required that's missing."""
        missing = []
        if not cls.SCALEKIT_ENV_URL:
            missing.append("SCALEKIT_ENV_URL")
        if not cls.SCALEKIT_CLIENT_ID:
            missing.append("SCALEKIT_CLIENT_ID")
        if not cls.SCALEKIT_CLIENT_SECRET:
            missing.append("SCALEKIT_CLIENT_SECRET")
        if missing:
            raise ValueError(
                "Missing required Scalekit settings: "
                + ", ".join(missing)
                + ". Copy .env.example to .env and fill these in."
            )
