"""
sk_gateway.py - Scalekit AgentKit MCP Gateway Provisioning

Builds the pieces of a working Hermes <-> AgentKit gateway connection:

1. A connected account for the GitHub connector (one-time user authorization).
2. An MCP config that maps that connection to a narrow set of tools.
3. A per-user MCP instance (its own MCP server URL).
4. A short-lived session token to authenticate Hermes's requests to that URL.

This is the layer Hermes Agent's config.yaml ultimately points at — Hermes
never sees a GitHub token, only a Scalekit-issued MCP session token that
expires and gets re-minted, same as any other bearer credential.

All calls here go through client.actions / client.actions.mcp, the same
typed action layer documented in Scalekit's AgentKit quickstart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from scalekit import ScalekitClient
from scalekit.actions.models.mcp_config import McpConfigConnectionToolMapping

from settings import Settings

logger = logging.getLogger("sk_gateway")
if not logger.handlers:
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")


@dataclass
class GatewayResult:
    """Everything needed to point Hermes at the provisioned gateway."""

    connected_account_status: str
    authorization_link: Optional[str]
    mcp_config_id: str
    mcp_config_name: str
    mcp_server_url: str  # config-level URL (shared shape for this config)
    instance_url: str  # per-user instance URL — this is what Hermes should call
    session_token: str
    session_token_expires_at: str


class ScalekitGateway:
    """Provisions and re-provisions the Hermes-facing MCP gateway, idempotently."""

    def __init__(self) -> None:
        Settings.validate()
        self.client = ScalekitClient(
            env_url=Settings.SCALEKIT_ENV_URL,
            client_id=Settings.SCALEKIT_CLIENT_ID,
            client_secret=Settings.SCALEKIT_CLIENT_SECRET,
        )
        logger.info("gateway.client_initialized env_url=%s", Settings.SCALEKIT_ENV_URL)

    # ------------------------------------------------------------------
    # Step 1: connected account (one-time user authorization)
    # ------------------------------------------------------------------
    def ensure_connected_account(self, identifier: str) -> tuple[str, Optional[str]]:
        """
        Create the connected account if it doesn't exist yet, and return its
        current status plus an authorization link if the user still needs to
        approve access.

        Returns:
            (status, authorization_link_or_None)
        """
        connection_name = Settings.SCALEKIT_GITHUB_CONNECTION

        response = self.client.actions.get_or_create_connected_account(
            connection_name=connection_name,
            identifier=identifier,
        )
        account = response.connected_account
        status = (account.status if account else None) or "unknown"
        logger.info(
            "gateway.connected_account status=%s identifier=%s connection=%s",
            status, identifier, connection_name,
        )

        if status.lower() in ("active",):
            return status, None

        # Not active yet (PENDING_AUTH, EXPIRED, DISCONNECTED, etc.) — mint a
        # fresh authorization link for the human to click.
        link_response = self.client.actions.get_authorization_link(
            connection_name=connection_name,
            identifier=identifier,
        )
        logger.info("gateway.authorization_link_minted identifier=%s", identifier)
        return status, link_response.link

    # ------------------------------------------------------------------
    # Step 2: MCP config (connector -> tool mapping)
    # ------------------------------------------------------------------
    def ensure_mcp_config(self) -> tuple[str, str]:
        """
        Reuse an existing MCP config with the configured name, or create one.

        Returns:
            (config_id, mcp_server_url)
        """
        existing = self.client.actions.mcp.list_configs(filter_name=Settings.MCP_CONFIG_NAME)
        for cfg in existing.configs:
            if cfg.name == Settings.MCP_CONFIG_NAME:
                logger.info("gateway.mcp_config_reused id=%s name=%s", cfg.id, cfg.name)
                return cfg.id, cfg.mcp_server_url or ""

        mapping = McpConfigConnectionToolMapping(
            connection_name=Settings.SCALEKIT_GITHUB_CONNECTION,
            tools=Settings.MCP_TOOLS,
        )
        created = self.client.actions.mcp.create_config(
            name=Settings.MCP_CONFIG_NAME,
            description="Hermes Agent gateway: GitHub tools via Scalekit AgentKit",
            connection_tool_mappings=[mapping],
        )
        cfg = created.config
        logger.info(
            "gateway.mcp_config_created id=%s name=%s tools=%s",
            cfg.id, cfg.name, Settings.MCP_TOOLS,
        )
        return cfg.id, cfg.mcp_server_url or ""

    # ------------------------------------------------------------------
    # Step 3: per-user MCP instance
    # ------------------------------------------------------------------
    def ensure_instance(self, config_id: str, identifier: str) -> str:
        """Ensure a per-user MCP instance exists; return its endpoint URL."""
        result = self.client.actions.mcp.ensure_instance(
            config_name=Settings.MCP_CONFIG_NAME,
            user_identifier=identifier,
        )
        instance = result.instance
        logger.info(
            "gateway.mcp_instance_ready id=%s user=%s url=%s",
            instance.id, instance.user_identifier, instance.url,
        )
        return instance.url or ""

    # ------------------------------------------------------------------
    # Step 4: session token for Hermes to authenticate with
    # ------------------------------------------------------------------
    def mint_session_token(self, config_id: str, identifier: str) -> tuple[str, str]:
        """Mint a bearer token Hermes's config.yaml can use against the instance URL."""
        response = self.client.actions.mcp.create_session_token(
            mcp_config_id=config_id,
            identifier=identifier,
            expiry=timedelta(hours=Settings.SESSION_TOKEN_TTL_HOURS),
        )
        logger.info(
            "gateway.session_token_minted identifier=%s expires_at=%s",
            identifier, response.expires_at,
        )
        return response.token or "", str(response.expires_at or "")

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------
    def provision(self, identifier: str) -> GatewayResult:
        """Run all four steps and return everything Hermes needs."""
        status, auth_link = self.ensure_connected_account(identifier)
        config_id, config_url = self.ensure_mcp_config()
        instance_url = self.ensure_instance(config_id, identifier)

        # A session token is only meaningful once the connected account is
        # active — minting one earlier just gives Hermes a token that will
        # fail on first tool call. Still mint it: it's what a re-run needs
        # once the human finishes the authorization link below.
        token, expires_at = self.mint_session_token(config_id, identifier)

        return GatewayResult(
            connected_account_status=status,
            authorization_link=auth_link,
            mcp_config_id=config_id,
            mcp_config_name=Settings.MCP_CONFIG_NAME,
            mcp_server_url=config_url,
            instance_url=instance_url,
            session_token=token,
            session_token_expires_at=expires_at,
        )
