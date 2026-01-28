"""Slack client for sending escalation notifications."""
import logging
from typing import Optional, Dict, Any, List
import httpx

logger = logging.getLogger(__name__)


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
        """Send message via incoming webhook."""
        payload = {"text": text}
        if blocks:
            payload["blocks"] = blocks
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(self.webhook_url, json=payload)
                response.raise_for_status()
                logger.info("Sent Slack message via webhook")
                return True
            except httpx.HTTPError as e:
                logger.error(f"Error sending Slack webhook: {e}")
                return False
    
    async def _send_via_bot(
        self, 
        text: str, 
        blocks: Optional[List[Dict[str, Any]]] = None,
        channel: str = None
    ) -> bool:
        """Send message via Bot Token API."""
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
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                result = response.json()
                if result.get("ok"):
                    logger.info(f"Sent Slack message to {channel}")
                    return True
                else:
                    logger.error(f"Slack API error: {result.get('error')}")
                    return False
            except httpx.HTTPError as e:
                logger.error(f"Error sending Slack message: {e}")
                return False
    
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
