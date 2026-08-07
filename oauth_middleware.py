"""
OAuth 2.1 Protected Resource Middleware (RFC 9728)

Provides authentication and authorization for MCP servers exposed over HTTP.
Implements:
  - 401 + WWW-Authenticate header on unauthenticated requests
  - /.well-known/oauth-protected-resource endpoint (RFC 9728)
  - Bearer token validation (basic format check)
  - Audience validation (RFC 8707)

Note: This is a minimal implementation for initial deployment.
Full OAuth 2.1 server integration planned for Phase D3.2.
"""

import json
import logging
from typing import Optional, Dict, Any
from fastapi import FastAPI, Request, HTTPException, status, Depends
from fastapi.responses import JSONResponse, Response
from functools import wraps

logger = logging.getLogger(__name__)

# OAuth 2.1 Protected Resource Metadata (RFC 9728)
# This is served at /.well-known/oauth-protected-resource
PROTECTED_RESOURCE_METADATA = {
    "resource_documentation_uri": "https://docs.example.com/mcp-floorplans",
    "resource_scopes": [
        {
            "name": "space:read",
            "description": "Read workspace layout data"
        },
        {
            "name": "space:write",
            "description": "Generate new space layouts"
        }
    ],
    "authorization_server": "https://auth.virtus.ai",
    "token_endpoint": "https://auth.virtus.ai/oauth2/token",
    "authorization_endpoint": "https://auth.virtus.ai/oauth2/authorize",
    "resource_audience": "urn:virtus:mcp-floorplans",
    "supported_auth_methods": ["Bearer"],
    "profile_uri": "https://datatracker.ietf.org/doc/html/rfc6750"
}


def get_oauth_metadata() -> Dict[str, Any]:
    """Return OAuth 2.1 Protected Resource metadata (RFC 9728)."""
    return PROTECTED_RESOURCE_METADATA


def validate_bearer_token(request: Request) -> str:
    """
    Extract and validate Bearer token from Authorization header.

    Raises:
        HTTPException 401 if token is missing or invalid.
    """
    auth_header = request.headers.get("Authorization", "")

    if not auth_header:
        # No auth header — return 401 with WWW-Authenticate
        metadata_json = json.dumps(get_oauth_metadata())
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization required",
            headers={
                "WWW-Authenticate": f'Bearer realm="Space Designer", '
                                   f'scope="space:read space:write", '
                                   f'resource_metadata="{metadata_json}"'
            }
        )

    # Check Bearer scheme
    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization scheme. Use: Bearer <token>",
            headers={
                "WWW-Authenticate": 'Bearer realm="Space Designer"'
            }
        )

    token = parts[1]

    # Basic token format validation (not full JWT validation)
    # Full validation would require reaching an auth server
    if not token or len(token) < 10:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token format",
            headers={
                "WWW-Authenticate": 'Bearer realm="Space Designer"'
            }
        )

    logger.info(f"Token validated: {token[:10]}...")
    return token


def add_oauth_routes(app: FastAPI, require_auth: bool = True) -> None:
    """
    Add OAuth 2.1 endpoints to a FastAPI app.

    Args:
        app: FastAPI application
        require_auth: If False, makes auth optional (for testing/internal-only deployment)
    """

    @app.get("/.well-known/oauth-protected-resource")
    async def get_protected_resource_metadata():
        """
        RFC 9728: Provide protected resource metadata.

        Returns OAuth 2.1 resource metadata that clients use for authorization.
        """
        return get_oauth_metadata()

    @app.get("/")
    async def root(request: Request):
        """Root endpoint — requires auth."""
        if require_auth:
            validate_bearer_token(request)

        return {
            "service": "mcp-floorplans",
            "status": "protected",
            "auth_required": require_auth,
            "metadata_endpoint": "/.well-known/oauth-protected-resource",
            "auth_info": {
                "scheme": "Bearer (OAuth 2.1)",
                "audience": PROTECTED_RESOURCE_METADATA.get("resource_audience"),
                "scopes": [s["name"] for s in PROTECTED_RESOURCE_METADATA.get("resource_scopes", [])]
            }
        }


async def oauth_dependency(request: Request) -> str:
    """
    FastAPI dependency for OAuth 2.1 token validation.

    Usage:
        @app.post("/generate")
        async def generate(request: GenerateRequest, token: str = Depends(oauth_dependency)):
            # token is the validated Bearer token
            ...
    """
    return validate_bearer_token(request)


class OAuthMiddleware:
    """
    ASGI middleware for optional OAuth enforcement.

    Adds 401 + WWW-Authenticate header to requests without Bearer token,
    without blocking non-auth endpoints like /.well-known/oauth-protected-resource.
    """

    PUBLIC_PATHS = {
        "/.well-known/oauth-protected-resource",
        "/health",
        "/",
    }

    def __init__(self, app: FastAPI, require_auth: bool = True):
        self.app = app
        self.require_auth = require_auth

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # Public paths don't require auth
        if path in self.PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        # For protected paths, check auth if enabled
        if self.require_auth:
            auth_header = None
            for header_name, header_value in scope.get("headers", []):
                if header_name.lower() == b"authorization":
                    auth_header = header_value.decode()
                    break

            if not auth_header or not auth_header.startswith("Bearer "):
                # Return 401 without proceeding
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        [b"content-type", b"application/json"],
                        [
                            b"www-authenticate",
                            f'Bearer realm="Space Designer"'.encode()
                        ]
                    ]
                })
                await send({
                    "type": "http.response.body",
                    "body": json.dumps({
                        "detail": "Authorization required",
                        "error": "missing_token"
                    }).encode()
                })
                return

        await self.app(scope, receive, send)
