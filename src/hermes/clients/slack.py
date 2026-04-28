"""Slack client for sending escalation notifications."""
import logging
from typing import Optional, Dict, Any, List
import httpx

from hermes.utils.metrics import ExternalService, track_call

logger = logging.getLogger(__name__)


class SlackApiError(Exception):
    """
    Raised when the Slack Web API returns ``ok=false`` for a request.

    Slack returns an HTTP 200 with ``{"ok": false, "error": "..."}`` for
    application-level failures (``invalid_auth``, ``channel_not_found``,
    ``not_in_channel``, rate limits, ...). Without an exception here those
    failures slip past :func:`track_call` and silently get recorded as
    ``EXTERNAL_CALL_DURATION{service="slack", status="success"}`` while
    ``EXTERNAL_CALL_ERRORS`` stays flat. Raising propagates the failure so
    ``track_call`` records the error and the caller in
    :meth:`hermes.core.remediation_manager.RemediationManager._escalate` can
    set ``ESCALATIONS_SENT{result="failed"}`` and
    ``SLACK_NOTIFICATIONS{status="error"}`` correctly.

    The class name is bounded (closed-set ``error_type`` label value), so
    cardinality stays safe.
    """


class SlackClient:
    """Client for sending Slack notifications for escalations."""
    
    def __init__(
        self, 
        webhook_url: Optional[str] = None,
        bot_token: Optional[str] = None,
        noc_channel: str = "#noc-alerts",
        noc_user_group: str = "noc-on-call"
    ):
        self.webhook_url = webhook_url
        self.bot_token = bot_token
        self.noc_channel = noc_channel
        self.noc_user_group = noc_user_group
        
        if not webhook_url and not bot_token:
            logger.warning("No Slack webhook or bot token configured")
    
    async def send_message(
        self,
        text: str,
        blocks: Optional[List[Dict[str, Any]]] = None,
        channel: Optional[str] = None
    ) -> bool:
        """
        Send a message to Slack.

        Uses webhook if available, otherwise bot token.

        Returns:
            ``True`` on success.

        Raises:
            httpx.HTTPError: transport/HTTP-level failure.
            SlackApiError: Slack returned ``ok=false``.

        Returning ``False`` (instead of raising) for the unconfigured branch
        is intentional: that's a static configuration condition, not a
        dependency failure, and it never enters :func:`track_call`.
        """
        if self.webhook_url:
            return await self._send_via_webhook(text, blocks)
        elif self.bot_token:
            return await self._send_via_bot(text, blocks, channel or self.noc_channel)
        else:
            logger.error("Cannot send Slack message: no webhook or bot token configured")
            return False

    async def _send_via_webhook(
        self,
        text: str,
        blocks: Optional[List[Dict[str, Any]]] = None
    ) -> bool:
        """
        Send message via incoming webhook.

        Re-raises on failure (does **not** swallow the exception and return
        ``False``). The previous swallow-and-return-False behavior caused
        :func:`track_call` to fall through to the success branch and record
        ``EXTERNAL_CALL_DURATION{service="slack", status="success"}`` for
        every failed Slack call, while ``EXTERNAL_CALL_ERRORS`` stayed flat.
        Aligning with :class:`hermes.clients.jira.JiraClient` and
        :class:`hermes.clients.alertmanager.AlertmanagerClient`, which both
        re-raise out of ``track_call``.
        """
        payload = {"text": text}
        if blocks:
            payload["blocks"] = blocks

        async with track_call(ExternalService.SLACK, "webhook_post"):
            async with httpx.AsyncClient() as client:
                try:
                    response = await client.post(self.webhook_url, json=payload)
                    response.raise_for_status()
                except httpx.HTTPError as e:
                    logger.error(f"Error sending Slack webhook: {e}")
                    raise
                logger.info("Sent Slack message via webhook")
                return True

    async def _send_via_bot(
        self,
        text: str,
        blocks: Optional[List[Dict[str, Any]]] = None,
        channel: str = None
    ) -> bool:
        """
        Send message via Bot Token API.

        Two failure modes, both re-raised so :func:`track_call` records the
        error:

        - HTTP-level failure (httpx raises) — re-raised verbatim.
        - Slack API-level failure (HTTP 200 with ``{"ok": false, ...}``) —
          surfaced as :class:`SlackApiError`. This is the class of bug the
          old swallow-and-return-False path hid: the call wire-succeeded but
          Slack refused to deliver the message, and we need that to count as
          a service error in metrics and to propagate up to ``_escalate`` so
          ``ESCALATIONS_SENT{result="failed"}`` increments.
        """
        url = "https://slack.com/api/chat.postMessage"
        headers = {
            "Authorization": f"Bearer {self.bot_token}",
            "Content-Type": "application/json"
        }

        payload = {
            "channel": channel or self.noc_channel,
            "text": text
        }
        if blocks:
            payload["blocks"] = blocks

        async with track_call(ExternalService.SLACK, "chat_post_message"):
            async with httpx.AsyncClient() as client:
                try:
                    response = await client.post(url, headers=headers, json=payload)
                    response.raise_for_status()
                    result = response.json()
                except httpx.HTTPError as e:
                    logger.error(f"Error sending Slack message: {e}")
                    raise
                if not result.get("ok"):
                    error = result.get("error", "unknown")
                    logger.error(f"Slack API error: {error}")
                    raise SlackApiError(f"Slack API returned ok=false: {error}")
                logger.info(f"Sent Slack message to {channel}")
                return True
    
    async def send_escalation(
        self,
        alert_name: str,
        alert_labels: Dict[str, str],
        reason: str,
        jira_ticket_id: Optional[str] = None,
        jira_url: Optional[str] = None,
        rundeck_url: Optional[str] = None
    ) -> bool:
        """
        Send an escalation notification to NOC.
        
        Includes @mention of NOC user group.
        """
        # Build rich formatted message using Slack blocks
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🚨 Alert Escalation - Manual Intervention Required",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"<!subteam^{self.noc_user_group}> - Automated remediation was unsuccessful."
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Alert:*\n{alert_name}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Reason:*\n{reason}"
                    }
                ]
            }
        ]
        
        # Add labels section
        if alert_labels:
            label_text = "\n".join([f"• `{k}`: {v}" for k, v in alert_labels.items()])
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Labels:*\n{label_text}"
                }
            })
        
        # Add links section
        links = []
        if jira_ticket_id:
            link = jira_url or jira_ticket_id
            links.append(f"<{jira_url}|JIRA: {jira_ticket_id}>" if jira_url else f"JIRA: {jira_ticket_id}")
        if rundeck_url:
            links.append(f"<{rundeck_url}|Rundeck Execution>")
        
        if links:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Links:*\n" + " | ".join(links)
                }
            })
        
        blocks.append({"type": "divider"})
        
        # Fallback text for notifications
        text = f"🚨 Alert Escalation: {alert_name} - {reason}. @{self.noc_user_group} please investigate."
        
        return await self.send_message(text, blocks)
    
    async def send_remediation_success(
        self,
        alert_name: str,
        jira_ticket_id: Optional[str] = None
    ) -> bool:
        """Send a success notification (optional, for visibility)."""
        text = f"✅ Auto-remediation successful for *{alert_name}*"
        if jira_ticket_id:
            text += f" (JIRA: {jira_ticket_id})"
        
        return await self.send_message(text)
