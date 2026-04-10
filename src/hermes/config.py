"""
Configuration models for Hermes.

Handles loading and validation of configuration from YAML files.
"""
import yaml
import os
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class AlertRemediationConfig(BaseModel):
    """Per-alert remediation configuration."""
    enabled: bool = Field(default=True)
    alertmanager_check_delay_minutes: Optional[int] = Field(default=None, description="Minutes to wait after job success before checking Alertmanager for resolution")
    job_retrigger_cooldown_minutes: Optional[int] = Field(default=None, description="Override global cooldown (min time before retriggering same Rundeck job)")
    max_attempts: Optional[int] = Field(default=None, description="Override global max retry attempts for this alert")
    jira_ticket_option: str = Field(default="jira_ticket", description="Rundeck option name for JIRA ticket")
    # JIRA ticket fetching - for alerts where ticket is created by jira-alert service
    fetch_jira_ticket: bool = Field(default=False, description="Fetch JIRA ticket ID via JQL before triggering Rundeck")
    jira_summary_search_field: Optional[str] = Field(default=None, description="Alert field to use for JIRA summary search (e.g., 'dataflow_id')")
    # Skip alert resolution check - for alerts that won't auto-resolve (e.g., customer action required)
    # When True: still monitors job execution and escalates on failure, but skips waiting for alert resolution
    skip_resolution_check: bool = Field(default=False, description="Skip alert resolution check after job success (for alerts that require customer action)")
    # Alert payload forwarding - send full alert context to Rundeck job
    send_alert_payload: bool = Field(default=False, description="Send full alert payload as JSON string to Rundeck job")
    alert_payload_option_name: str = Field(default="alert_payload", description="Rundeck option name for alert payload JSON")
    # Static options - always sent to Rundeck job regardless of alert payload
    static_options: Dict[str, str] = Field(default_factory=dict, description="Static key-value pairs always passed to Rundeck job options")


class AlertConfig(BaseModel):
    required_fields: List[str] = Field(default_factory=list, alias="required_fields")
    job_id: str = Field(..., alias="job_id")
    fields_location: str = Field(default="commonLabels", alias="fields_location")
    remediation: AlertRemediationConfig = Field(default_factory=AlertRemediationConfig)
    field_mappings: Dict[str, str] = Field(default_factory=dict, alias="field_mappings")
    # Value mappings - conditionally set Rundeck options based on alert label values
    # Structure: {source_field: {field_value: {rundeck_option: option_value}}}
    # Example: {"region": {"us-west-2": {"cluster": "us-west-2-prod-new"}}}
    value_mappings: Dict[str, Dict[str, Dict[str, str]]] = Field(
        default_factory=dict, 
        alias="value_mappings",
        description="Conditional mappings based on alert label values"
    )
    # Job ID mappings - conditionally route to different Rundeck jobs based on alert field values
    # Structure: {source_field: {field_value: job_id}}
    # Example: {"error_message": {"Data-collection-vendor-is-down": "job-uuid-1", "Active-dataflow-failed": "job-uuid-2"}}
    job_id_mappings: Dict[str, Dict[str, str]] = Field(
        default_factory=dict,
        alias="job_id_mappings",
        description="Conditional job routing based on alert field values"
    )


class RemediationConfig(BaseModel):
    """Global remediation settings."""
    poll_interval_seconds: int = Field(default=30, description="How often to check job status")
    alertmanager_check_delay_minutes: int = Field(default=5, description="Minutes to wait after job success before checking Alertmanager for resolution")
    max_job_wait_minutes: int = Field(default=30, description="Timeout for job completion")
    alert_check_interval_seconds: int = Field(default=30, description="How often to poll Alertmanager during resolution wait")
    job_retrigger_cooldown_minutes: int = Field(default=5, description="Minimum minutes before retriggering same Rundeck job for same alert")
    max_attempts: int = Field(default=2, description="Default max retry attempts for remediation (initial + retries)")
    max_concurrent_workflows: int = Field(default=100, description="Maximum number of concurrent remediation workflows")
    circuit_breaker_failure_threshold: int = Field(default=5, description="Number of failures before opening circuit breaker")
    circuit_breaker_recovery_seconds: int = Field(default=60, description="Time to wait before retrying after circuit breaker opens")
    # Rate limiting
    rate_limit_enabled: bool = Field(default=True, description="Enable rate limiting per Alertmanager source")
    rate_limit_per_source_rate: float = Field(default=10.0, description="Requests per second per Alertmanager")
    rate_limit_per_source_burst: int = Field(default=50, description="Max burst per Alertmanager")
    rate_limit_global_rate: float = Field(default=100.0, description="Total requests per second")
    rate_limit_global_burst: int = Field(default=500, description="Total max burst")


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


class ApiConfig(BaseModel):
    """API authentication settings for public endpoints."""
    webhook_api_key: Optional[str] = Field(default=None, description="API key for public webhook endpoint authentication")


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
    """Main configuration container."""
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
    api: Optional[ApiConfig] = Field(default=None, description="API authentication settings")

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
        return True  # Always enable - we get Alertmanager URL from alert payload

    def create_rundeck_client(self):
        """Create a RundeckClient instance based on configuration."""
        from hermes.clients.rundeck import RundeckClient
        
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
    """Load configuration from a YAML file."""
    try:
        with open(filename, 'r') as f:
            data = yaml.safe_load(f)
        
        # Handle the alert configs to set default fields_location if missing
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
