"""Rundeck API client with session-based authentication.

Supports both:
- API Token authentication (legacy, max 30 days)
- Username/Password authentication with session cookie (no expiration)
"""
import logging
from typing import Optional, Dict, Any
import httpx

from hermes.utils.metrics import ExternalService, track_call

logger = logging.getLogger(__name__)


#TODO: figure out how to get API token with no expiration so that we can deprecate session auth, its bad and ugly


class RundeckLoginError(Exception):
    """
    Raised when Rundeck login completes at the HTTP level but does not
    establish a usable session.

    The classic case: ``j_security_check`` returns 200 (after following
    redirects) but the response carries no ``JSESSIONID`` cookie because the
    credentials were rejected. Without an exception here that condition slips
    past :func:`track_call` and silently records
    ``EXTERNAL_CALL_DURATION{service="rundeck", operation="login",
    status="success"}`` while ``EXTERNAL_CALL_ERRORS`` stays flat. Raising
    propagates the failure so the metric is attributed correctly and so that
    callers (``_request``) get a clear exception instead of a stale ``False``.

    The class name is bounded (closed-set ``error_type`` label value), so
    cardinality stays safe.
    """


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
        """
        Authenticate with Rundeck and obtain a session cookie.

        Returns:
            ``True`` on success (or when token auth is in use and no login
            is required).

        Raises:
            httpx.HTTPError: transport/HTTP-level failure during the login
                request. Re-raised so :func:`track_call` records the error.
            RundeckLoginError: the login request succeeded at the HTTP level
                but no ``JSESSIONID`` cookie was returned (typically a
                credential rejection). Re-raised for the same reason.

        The previous implementation swallowed both failure modes and
        returned ``False``, which caused :func:`track_call` to record every
        failed login as ``status="success"``. See :class:`RundeckLoginError`
        for the full rationale.
        """
        if not self._use_session_auth:
            return True  # Token auth doesn't need login

        logger.info("Authenticating with Rundeck using username/password...")

        async with track_call(ExternalService.RUNDECK, "login"):
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
                except httpx.HTTPError as e:
                    logger.error(f"Failed to authenticate with Rundeck: {e}")
                    raise

                # Extract JSESSIONID cookie from response cookies
                # Check both response cookies and client cookies
                self._session_cookie = response.cookies.get("JSESSIONID") or client.cookies.get("JSESSIONID")

                if not self._session_cookie:
                    logger.error("Failed to obtain session cookie from Rundeck")
                    raise RundeckLoginError(
                        "Login response did not include a JSESSIONID cookie"
                    )

                logger.info(
                    f"Successfully authenticated with Rundeck "
                    f"(session: {self._session_cookie[:8]}...)"
                )
                return True
    
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
        
        # Ensure we have a session for session-based auth.
        # ``_login`` now raises (httpx.HTTPError / RundeckLoginError) on
        # failure rather than returning False, so propagating directly is
        # both correct and lets the inner login ``track_call`` attribute the
        # error to operation="login".
        if self._use_session_auth and not self._session_cookie:
            await self._login()

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
                
                # Handle session expiry - re-authenticate and retry.
                # Rundeck returns 401 or 403 when session is expired/invalid.
                # ``_login`` raises on failure, so on a successful return we
                # always have a fresh session and can replay the request.
                if response.status_code in (401, 403) and self._use_session_auth and retry_on_auth_failure:
                    logger.warning(f"Session expired or unauthorized ({response.status_code}), re-authenticating...")
                    self._session_cookie = None
                    await self._login()
                    return await self._request(method, endpoint, json_data, retry_on_auth_failure=False)
                
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
        async with track_call(ExternalService.RUNDECK, "run_job"):
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
        async with track_call(ExternalService.RUNDECK, "get_execution"):
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
        async with track_call(ExternalService.RUNDECK, "get_execution_output"):
            return await self._request(
                method="GET",
                endpoint=f"/execution/{execution_id}/output"
            )
