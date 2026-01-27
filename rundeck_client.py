"""Rundeck API client with session-based authentication.

Supports both:
- API Token authentication (legacy, max 30 days)
- Username/Password authentication with session cookie (no expiration)
"""
import logging
from typing import Optional, Dict, Any
import httpx

logger = logging.getLogger(__name__)


class RundeckClient:
    """Async Rundeck API client with automatic session management."""
    
    def __init__(
        self,
        base_url: str,
        # Token auth (legacy)
        auth_token: Optional[str] = None,
        # Session auth (preferred)
        username: Optional[str] = None,
        password: Optional[str] = None,
        # Options
        api_version: int = 52,
        timeout: float = 30.0,
        verify_ssl: bool = True
    ):
        self.base_url = base_url.rstrip('/')
        self.auth_token = auth_token
        self.username = username
        self.password = password
        self.api_version = api_version
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        
        # Session cookie storage
        self._session_cookie: Optional[str] = None
        
        # Determine auth method
        self._use_session_auth = bool(username and password)
        
        if not self._use_session_auth and not auth_token:
            raise ValueError("Either auth_token or username/password must be provided")
        
        if self._use_session_auth:
            logger.info("RundeckClient configured for session-based authentication")
        else:
            logger.info("RundeckClient configured for token-based authentication")
    
    async def _login(self) -> bool:
        """Authenticate with Rundeck and obtain session cookie."""
        if not self._use_session_auth:
            return True  # Token auth doesn't need login
        
        logger.info("Authenticating with Rundeck using username/password...")
        
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=self.timeout) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/j_security_check",
                    data={
                        "j_username": self.username,
                        "j_password": self.password
                    },
                    follow_redirects=True
                )
                
                # Extract JSESSIONID cookie from response cookies
                # Check both response cookies and client cookies
                self._session_cookie = response.cookies.get("JSESSIONID") or client.cookies.get("JSESSIONID")
                
                if self._session_cookie:
                    logger.info(f"Successfully authenticated with Rundeck (session: {self._session_cookie[:8]}...)")
                    return True
                else:
                    logger.error("Failed to obtain session cookie from Rundeck")
                    return False
                    
            except httpx.HTTPError as e:
                logger.error(f"Failed to authenticate with Rundeck: {e}")
                return False
    
    def _get_headers(self) -> Dict[str, str]:
        """Get headers for API requests."""
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        
        # Use token auth if not using session auth
        if not self._use_session_auth and self.auth_token:
            headers["X-Rundeck-Auth-Token"] = self.auth_token
        
        return headers
    
    def _get_cookies(self) -> Dict[str, str]:
        """Get cookies for API requests."""
        if self._use_session_auth and self._session_cookie:
            return {"JSESSIONID": self._session_cookie}
        return {}
    
    async def _request(
        self,
        method: str,
        endpoint: str,
        json_data: Optional[Dict[str, Any]] = None,
        retry_on_auth_failure: bool = True
    ) -> Dict[str, Any]:
        """Make an authenticated request to Rundeck API."""
        
        # Ensure we have a session for session-based auth
        if self._use_session_auth and not self._session_cookie:
            if not await self._login():
                raise Exception("Failed to authenticate with Rundeck")
        
        url = f"{self.base_url}/api/{self.api_version}{endpoint}"
        
        async with httpx.AsyncClient(
            verify=self.verify_ssl,
            timeout=self.timeout,
            cookies=self._get_cookies()
        ) as client:
            try:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=self._get_headers(),
                    json=json_data
                )
                
                # Handle session expiry - re-authenticate and retry
                if response.status_code == 401 and self._use_session_auth and retry_on_auth_failure:
                    logger.warning("Session expired, re-authenticating...")
                    self._session_cookie = None
                    if await self._login():
                        return await self._request(method, endpoint, json_data, retry_on_auth_failure=False)
                    else:
                        raise Exception("Failed to re-authenticate with Rundeck")
                
                response.raise_for_status()
                response_json = response.json()
                
                # Check for Rundeck API error response
                if isinstance(response_json, dict) and response_json.get("error") is True:
                    error_msg = response_json.get("message", "Unknown Rundeck error")
                    raise Exception(f"Rundeck API error: {error_msg}")
                
                return response_json
                
            except httpx.HTTPError as e:
                logger.error(f"Rundeck API request failed: {e}")
                if hasattr(e, 'response') and e.response is not None:
                    logger.error(f"Response: {e.response.text}")
                raise
    
    async def run_job(self, job_id: str, options: Dict[str, Any]) -> Dict[str, Any]:
        """
        Trigger a Rundeck job.
        
        Args:
            job_id: The Rundeck job UUID
            options: Job options/parameters
            
        Returns:
            Execution details including 'id' and 'permalink'
        """
        return await self._request(
            method="POST",
            endpoint=f"/job/{job_id}/run",
            json_data={"options": options}
        )
    
    async def get_execution(self, execution_id: str) -> Dict[str, Any]:
        """
        Get execution status and details.
        
        Args:
            execution_id: The Rundeck execution ID
            
        Returns:
            Execution details including 'status'
        """
        return await self._request(
            method="GET",
            endpoint=f"/execution/{execution_id}"
        )
    
    async def get_execution_output(self, execution_id: str) -> Dict[str, Any]:
        """
        Get execution output/logs.
        
        Args:
            execution_id: The Rundeck execution ID
            
        Returns:
            Execution output
        """
        return await self._request(
            method="GET",
            endpoint=f"/execution/{execution_id}/output"
        )
