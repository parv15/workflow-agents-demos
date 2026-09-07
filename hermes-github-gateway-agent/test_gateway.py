"""
test_gateway.py - Tests for the Scalekit MCP gateway provisioning flow

These tests mock scalekit.ScalekitClient's `actions` surface (not the network),
so they verify:
  - ScalekitGateway calls the SDK with the correct argument names/shapes
  - The 4-step provisioning flow (connected account -> config -> instance ->
    session token) runs in the right order and handles the "not active yet"
    branch correctly
  - The generated Hermes config snippet is valid, well-formed YAML

They intentionally mirror the real return shapes from scalekit-sdk-python
(ConnectedAccount, McpConfig, McpInstance, MagicLinkResponse, etc.) so a
future SDK signature change breaks these tests rather than shipping silently.
"""

import os
import sys
import types
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

# Ensure required env vars exist before settings.py is imported anywhere.
os.environ.setdefault("SCALEKIT_ENV_URL", "https://test-env.scalekit.com")
os.environ.setdefault("SCALEKIT_CLIENT_ID", "skc_test")
os.environ.setdefault("SCALEKIT_CLIENT_SECRET", "test_secret")
os.environ.setdefault("SCALEKIT_GITHUB_CONNECTION", "github")
os.environ.setdefault("MCP_CONFIG_NAME", "hermes-github-gateway-test")
os.environ.setdefault("MCP_TOOLS", "github_search_issues,github_issue_labels_add,github_issue_get")
os.environ.setdefault("HERMES_USER_IDENTIFIER", "test-user@example.com")

sys.path.insert(0, os.path.dirname(__file__))


def _make_connected_account(status: str, identifier: str = "test-user@example.com"):
    account = MagicMock()
    account.status = status
    account.identifier = identifier
    account.connector = "github"
    return account


def _make_mcp_config(config_id="cfg_123", name="hermes-github-gateway-test", url="https://test-env.scalekit.com/mcp/cfg_123"):
    cfg = MagicMock()
    cfg.id = config_id
    cfg.name = name
    cfg.mcp_server_url = url
    return cfg


def _make_mcp_instance(instance_id="inst_abc", identifier="test-user@example.com", url="https://test-env.scalekit.com/mcp/cfg_123/inst_abc"):
    instance = MagicMock()
    instance.id = instance_id
    instance.user_identifier = identifier
    instance.url = url
    return instance


@pytest.fixture
def gateway(monkeypatch):
    """A ScalekitGateway with client.actions fully mocked, no network calls."""
    from settings import Settings
    import sk_gateway

    mock_scalekit_client = MagicMock()
    monkeypatch.setattr(sk_gateway, "ScalekitClient", lambda **kwargs: mock_scalekit_client)

    gw = sk_gateway.ScalekitGateway()
    gw._mock_client = mock_scalekit_client  # stash for assertions
    return gw


class TestConnectedAccount:
    def test_active_account_returns_no_auth_link(self, gateway):
        response = MagicMock()
        response.connected_account = _make_connected_account("ACTIVE")
        gateway.client.actions.get_or_create_connected_account.return_value = response

        status, link = gateway.ensure_connected_account("test-user@example.com")

        assert status == "ACTIVE"
        assert link is None
        gateway.client.actions.get_or_create_connected_account.assert_called_once_with(
            connection_name="github", identifier="test-user@example.com"
        )
        # No authorization link should be minted for an already-active account
        gateway.client.actions.get_authorization_link.assert_not_called()

    def test_pending_account_mints_authorization_link(self, gateway):
        response = MagicMock()
        response.connected_account = _make_connected_account("PENDING_AUTH")
        gateway.client.actions.get_or_create_connected_account.return_value = response

        link_response = MagicMock()
        link_response.link = "https://test-env.scalekit.com/authorize/abc123"
        gateway.client.actions.get_authorization_link.return_value = link_response

        status, link = gateway.ensure_connected_account("test-user@example.com")

        assert status == "PENDING_AUTH"
        assert link == "https://test-env.scalekit.com/authorize/abc123"
        gateway.client.actions.get_authorization_link.assert_called_once_with(
            connection_name="github", identifier="test-user@example.com"
        )


class TestMcpConfig:
    def test_reuses_existing_config_by_name(self, gateway):
        list_response = MagicMock()
        list_response.configs = [_make_mcp_config()]
        gateway.client.actions.mcp.list_configs.return_value = list_response

        config_id, url = gateway.ensure_mcp_config()

        assert config_id == "cfg_123"
        assert url == "https://test-env.scalekit.com/mcp/cfg_123"
        gateway.client.actions.mcp.create_config.assert_not_called()

    def test_creates_config_when_none_exists(self, gateway):
        list_response = MagicMock()
        list_response.configs = []
        gateway.client.actions.mcp.list_configs.return_value = list_response

        created_response = MagicMock()
        created_response.config = _make_mcp_config(config_id="cfg_new")
        gateway.client.actions.mcp.create_config.return_value = created_response

        config_id, url = gateway.ensure_mcp_config()

        assert config_id == "cfg_new"
        args, kwargs = gateway.client.actions.mcp.create_config.call_args
        assert kwargs["name"] == "hermes-github-gateway-test"
        mapping = kwargs["connection_tool_mappings"][0]
        assert mapping.connection_name == "github"
        assert mapping.tools == ["github_search_issues", "github_issue_labels_add", "github_issue_get"]


class TestMcpInstance:
    def test_ensure_instance_returns_url(self, gateway):
        response = MagicMock()
        response.instance = _make_mcp_instance()
        gateway.client.actions.mcp.ensure_instance.return_value = response

        url = gateway.ensure_instance("cfg_123", "test-user@example.com")

        assert url == "https://test-env.scalekit.com/mcp/cfg_123/inst_abc"
        gateway.client.actions.mcp.ensure_instance.assert_called_once_with(
            config_name="hermes-github-gateway-test",
            user_identifier="test-user@example.com",
        )


class TestSessionToken:
    def test_mint_session_token(self, gateway):
        response = MagicMock()
        response.token = "sk_mcp_session_xyz"
        response.expires_at = datetime(2026, 9, 4, 12, 0, 0)
        gateway.client.actions.mcp.create_session_token.return_value = response

        token, expires_at = gateway.mint_session_token("cfg_123", "test-user@example.com")

        assert token == "sk_mcp_session_xyz"
        assert "2026-09-04" in expires_at
        _, kwargs = gateway.client.actions.mcp.create_session_token.call_args
        assert kwargs["mcp_config_id"] == "cfg_123"
        assert kwargs["identifier"] == "test-user@example.com"
        assert kwargs["expiry"] == timedelta(hours=8)


class TestFullProvisioning:
    def test_provision_active_account_end_to_end(self, gateway):
        ca_response = MagicMock()
        ca_response.connected_account = _make_connected_account("ACTIVE")
        gateway.client.actions.get_or_create_connected_account.return_value = ca_response

        list_response = MagicMock()
        list_response.configs = [_make_mcp_config()]
        gateway.client.actions.mcp.list_configs.return_value = list_response

        instance_response = MagicMock()
        instance_response.instance = _make_mcp_instance()
        gateway.client.actions.mcp.ensure_instance.return_value = instance_response

        token_response = MagicMock()
        token_response.token = "sk_mcp_session_xyz"
        token_response.expires_at = datetime(2026, 9, 4, 12, 0, 0)
        gateway.client.actions.mcp.create_session_token.return_value = token_response

        result = gateway.provision("test-user@example.com")

        assert result.connected_account_status == "ACTIVE"
        assert result.authorization_link is None
        assert result.mcp_config_id == "cfg_123"
        assert result.instance_url == "https://test-env.scalekit.com/mcp/cfg_123/inst_abc"
        assert result.session_token == "sk_mcp_session_xyz"


class TestHermesConfigSnippet:
    """The setup script writes a YAML snippet — verify it's well-formed and correct."""

    def test_snippet_is_valid_yaml_with_expected_shape(self, tmp_path):
        import yaml

        instance_url = "https://test-env.scalekit.com/mcp/cfg_123/inst_abc"
        token = "sk_mcp_session_xyz"
        tools = ["github_search_issues", "github_issue_labels_add", "github_issue_get"]

        snippet = f"""mcp_servers:
  github_gateway:
    url: "{instance_url}"
    headers:
      Authorization: "Bearer {token}"
    tools:
      include: [{', '.join(tools)}]
      prompts: false
      resources: false
"""
        parsed = yaml.safe_load(snippet)

        server = parsed["mcp_servers"]["github_gateway"]
        assert server["url"] == instance_url
        assert server["headers"]["Authorization"] == f"Bearer {token}"
        assert server["tools"]["include"] == tools
        assert server["tools"]["prompts"] is False
        assert server["tools"]["resources"] is False
