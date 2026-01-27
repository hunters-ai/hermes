import yaml
import os
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class AlertRemediationConfig(BaseModel):
    """Per-alert remediation configuration."""
    enabled: bool = Field(default=True)
    resolution_wait_minutes: Optional[int] = Field(default=None, description="Override global wait time")
    jira_ticket_option: str = Field(default="jira_ticket", description="Rundeck option name for JIRA ticket")


class AlertConfig(BaseModel):
    required_fields: List[str] = Field(default_factory=list, alias="required_fields")
    job_id: str = Field(..., alias="job_id")
    fields_location: str = Field(default="commonLabels", alias="fields_location")
    remediation: AlertRemediationConfig = Field(default_factory=AlertRemediationConfig)
    field_mappings: Dict[str, str] = Field(default_factory=dict, alias="field_mappings")


class RemediationConfig(BaseModel):
    """Global remediation settings."""
    poll_interval_seconds: int = Field(default=30, description="How often to check job status")
    resolution_wait_minutes: int = Field(default=5, description="Wait time after job success before checking alert")
    max_job_wait_minutes: int = Field(default=30, description="Timeout for job completion")
    alert_check_interval_seconds: int = Field(default=30, description="How often to poll Alertmanager during resolution wait")


class StateStoreConfig(BaseModel):
    """State store configuration."""
    type: str = Field(default="memory", description="State store type: 'memory' or 'dynamodb'")
    # DynamoDB settings
    dynamodb_table: str = Field(default="hunters-ops-hermes-workflows", description="DynamoDB table name")
    dynamodb_region: Optional[str] = Field(default=None, description="AWS region for DynamoDB")
    dynamodb_endpoint: Optional[str] = Field(default=None, description="Custom DynamoDB endpoint (for LocalStack)")
    ttl_hours: int = Field(default=24, description="TTL for completed workflows")


class AlertmanagerConfig(BaseModel):
    """Alertmanager integration settings."""
    base_url: str = Field(..., description="Alertmanager base URL")
    bearer_token: Optional[str] = Field(default=None, description="Optional auth token")


class JiraConfig(BaseModel):
    """JIRA integration settings."""
    base_url: str = Field(..., description="JIRA base URL (e.g., https://company.atlassian.net)")
    api_token: str = Field(..., description="JIRA API token")
    user_email: str = Field(..., description="JIRA user email for authentication")


class SlackConfig(BaseModel):
    """Slack integration settings."""
    webhook_url: Optional[str] = Field(default=None, description="Slack webhook URL")
    bot_token: Optional[str] = Field(default=None, description="Slack bot token for more control")
    noc_channel: str = Field(default="#noc-alerts", description="Channel for escalations")
    noc_user_group: str = Field(default="noc-on-call", description="User group for @mentions")


class RundeckConfig(BaseModel):
    """Rundeck connection settings - supports token or session-based auth."""
    base_url: str = Field(..., description="Rundeck base URL")
    
    # Token-based auth (legacy, max 30 days expiry)
    auth_token: Optional[str] = Field(default=None, description="API token (legacy)")
    
    # Session-based auth (preferred, no expiration)
    username: Optional[str] = Field(default=None, description="Rundeck username")
    password: Optional[str] = Field(default=None, description="Rundeck password")
    
    # Connection options
    api_version: int = Field(default=52, description="Rundeck API version")
    timeout: float = Field(default=30.0, description="Request timeout in seconds")
    verify_ssl: bool = Field(default=True, description="Verify SSL certificates")


class Config(BaseModel):
    # Rundeck settings - support both old format and new nested format
    auth_token: Optional[str] = Field(default=None, alias="auth_token")
    base_url: Optional[str] = Field(default=None, alias="base_url")
    rundeck: Optional[RundeckConfig] = Field(default=None, description="Rundeck config (new format)")
    
    # Alert configurations
    alert_configs: Dict[str, AlertConfig] = Field(default_factory=dict, alias="alerts")
    
    # New integration configs
    remediation: RemediationConfig = Field(default_factory=RemediationConfig)
    state_store: StateStoreConfig = Field(default_factory=StateStoreConfig)
    alertmanager: Optional[AlertmanagerConfig] = Field(default=None)
    jira: Optional[JiraConfig] = Field(default=None)
    slack: Optional[SlackConfig] = Field(default=None)

    def get_alert_config(self, alert_name: str) -> Optional[AlertConfig]:
        return self.alert_configs.get(alert_name)

    def get_rundeck_base_url(self) -> str:
        """Get Rundeck base URL, supporting both old and new config formats."""
        if self.rundeck:
            return self.rundeck.base_url
        return self.base_url or ""

    def get_rundeck_auth_token(self) -> Optional[str]:
        """Get Rundeck auth token, supporting both old and new config formats."""
        if self.rundeck:
            return self.rundeck.auth_token
        return self.auth_token

    def get_webhook_url(self, job_id: str) -> str:
        base = self.get_rundeck_base_url()
        api_version = self.rundeck.api_version if self.rundeck else 52
        return f"{base}/api/{api_version}/job/{job_id}/run"

    def get_execution_url(self, execution_id: str) -> str:
        """Get URL for fetching execution details."""
        base = self.get_rundeck_base_url()
        api_version = self.rundeck.api_version if self.rundeck else 52
        return f"{base}/api/{api_version}/execution/{execution_id}"

    def is_remediation_enabled(self) -> bool:
        """
        Check if remediation monitoring is enabled.
        
        In global service mode, Alertmanager URL is extracted from the alert's
        client_url field, so static alertmanager config is optional.
        """
        # Remediation is enabled if we have either:
        # 1. A static alertmanager configured, OR
        # 2. We trust that alerts will include client_url (global service mode)
        return True  # Always enable - we get Alertmanager URL from alert payload

    def create_rundeck_client(self):
        """Create a RundeckClient instance based on configuration.
        
        Uses session-based auth if username/password are provided,
        otherwise falls back to token-based auth.
        """
        from rundeck_client import RundeckClient
        
        if self.rundeck:
            return RundeckClient(
                base_url=self.rundeck.base_url,
                auth_token=self.rundeck.auth_token,
                username=self.rundeck.username,
                password=self.rundeck.password,
                api_version=self.rundeck.api_version,
                timeout=self.rundeck.timeout,
                verify_ssl=self.rundeck.verify_ssl
            )
        else:
            # Legacy config format
            return RundeckClient(
                base_url=self.base_url or "",
                auth_token=self.auth_token
            )


def load_alert_config(filename: str) -> Config:
    try:
        with open(filename, 'r') as f:
            data = yaml.safe_load(f)
        
        # Manually handle the alert configs to set default fields_location if missing
        if 'alerts' in data:
            for alert_name, alert_data in data['alerts'].items():
                if 'fields_location' not in alert_data:
                    alert_data['fields_location'] = 'commonLabels'
        
        # Handle environment variable substitution for secrets
        def substitute_env_vars(obj):
            if isinstance(obj, str):
                if obj.startswith("${") and obj.endswith("}"):
                    env_var = obj[2:-1]
                    return os.getenv(env_var, obj)
                return obj
            elif isinstance(obj, dict):
                return {k: substitute_env_vars(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [substitute_env_vars(item) for item in obj]
            return obj
        
        data = substitute_env_vars(data)
        
        return Config(**data)
    except Exception as e:
        raise RuntimeError(f"Failed to load config from {filename}: {e}")
