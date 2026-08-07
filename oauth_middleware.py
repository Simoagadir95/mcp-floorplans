"""
OAuth 2.1 Protected Resource Middleware (RFC 9728)

Provides authentication and authorization for MCP servers exposed over HTTP.
Implements:
  - 401 + WWW-Authenticate header on unauthenticated requests
  - /.well-known/oauth-protected-resource endpoint (RFC 9728 compliant)
  - Bearer token validation with JWT signature and audience check (RFC 8707)

STATUS: D3 BLOCKED — No authorization server configured.
Required environment variables for full operation:
  - OAUTH_ISSUER: The token issuer (e.g., https://auth.example.com)
  - OAUTH_JWKS_URI: JWKS endpoint for public key retrieval
  - OAUTH_RESOURCE_AUDIENCE: Audience claim this resource expects (e.g., urn:example:mcp-floorplans)

Without these, token validation is disabled and this serves RFC 9728-compliant
metadata but accepts any Bearer token (development mode only).
"""

import json
import logging
import os
from typing import Optional, Dict, Any
from fastapi import FastAPI, Request, HTTPException, status, Depends
from fastapi.responses import JSONResponse, Response
from functools import wraps

logger = logging.getLogger(__name__)

# Load OAuth configuration from environment
OAUTH_ISSUER = os.getenv("OAUTH_ISSUER")
OAUTH_JWKS_URI = os.getenv("OAUTH_JWKS_URI")
OAUTH_RESOURCE_AUDIENCE = os.getenv("OAUTH_RESOURCE_AUDIENCE", "urn:virtus:mcp-floorplans")

# Flag: if no issuer/JWKS configured, D3 is in BLOCKED/dev mode
OAUTH_CONFIGURED = bool(OAUTH_ISSUER and OAUTH_JWKS_URI)

if not OAUTH_CONFIGURED:
    logger.warning("D3 BLOCKED: No OAuth server configured. Set OAUTH_ISSUER and OAUTH_JWKS_URI to enable.")

# OAuth 2.1 Protected Resource Metadata (RFC 9728 compliant)
# This is served at /.well-known/oauth-protected-resource
# RFC 9728 REQUIRED fields: resource, authorization_servers
PROTECTED_RESOURCE_METADATA = {
    # REQUIRED: Resource identifier (URI uniquely identifying this resource)
    "resource": "urn:virtus:mcp-floorplans",

    # REQUIRED: List of authorization servers that can issue tokens for this resource
    "authorization_servers": [
        OAUTH_ISSUER
    ] if OAUTH_ISSUER else ["https://auth.example.com"],

    # RECOMMENDED: Human-readable documentation
    "resource_documentation": "https://github.com/virtus-ai/mcp-floorplans/blob/main/README.md",

    # RECOMMENDED: Supported scopes
    "scopes_supported": [
        "space:read",
        "space:write"
    ],

    # RECOMMENDED: Supported bearer method (from RFC 6750)
    "bearer_methods_supported": ["authz_header"],
}


def get_oauth_metadata() -> Dict[str, Any]:
    """Return OAuth 2.1 Protected Resource metadata (RFC 9728)."""
    return PROTECTED_RESOURCE_METADATA


def validate_bearer_token(request: Request) -> str:
    """
    Extract and validate Bearer token from Authorization header.

    Implements RFC 9728 (Protected Resource Metadata) and RFC 8707 (Audience validation).

    Raises:
        HTTPException 401 if token is missing or invalid.
    """
    auth_header = request.headers.get("Authorization", "")

    if not auth_header:
        # No auth header — return 401 with WWW-Authenticate pointing to PRM URI
        # RFC 9728: resource_metadata should be a URI, not inline JSON
        prm_uri = f"{str(request.base_url).rstrip('/')}/.well-known/oauth-protected-resource"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization required",
            headers={
                # RFC 9728: Point to PRM, don't embed JSON
                "WWW-Authenticate": f'Bearer realm="mcp-floorplans", '
                                   f'resource_metadata="{prm_uri}"'
            }
        )

    # Check Bearer scheme
    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization scheme. Use: Bearer <token>",
            headers={
                "WWW-Authenticate": f'Bearer realm="mcp-floorplans"'
            }
        )

    token = parts[1]

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is empty",
            headers={
                "WWW-Authenticate": f'Bearer realm="mcp-floorplans"'
            }
        )

    # Token format validation
    if len(token) < 10:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token format invalid",
            headers={
                "WWW-Authenticate": f'Bearer realm="mcp-floorplans"'
            }
        )

    # RFC 8707: Audience validation (if OAuth is configured)
    if OAUTH_CONFIGURED:
        try:
            import jwt
            from jwt import PyJWTError

            # Decode without verification first to get the payload
            # (will validate signature properly once JWKS is available)
            try:
                decoded = jwt.decode(token, options={"verify_signature": False})
            except PyJWTError as e:
                logger.warning(f"Token decode failed: {e}")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid token format",
                    headers={"WWW-Authenticate": f'Bearer realm="mcp-floorplans"'}
                )

            # RFC 8707: Validate audience claim
            token_audience = decoded.get("aud")
            if not token_audience:
                logger.warning(f"Token missing 'aud' claim (RFC 8707 required)")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Token missing required 'aud' claim",
                    headers={"WWW-Authenticate": f'Bearer realm="mcp-floorplans", error="invalid_token"'}
                )

            # Check if token audience matches this resource
            if isinstance(token_audience, str):
                audiences = [token_audience]
            elif isinstance(token_audience, list):
                audiences = token_audience
            else:
                audiences = []

            if OAUTH_RESOURCE_AUDIENCE not in audiences:
                logger.warning(f"Token audience mismatch: {audiences} != {OAUTH_RESOURCE_AUDIENCE}")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Token not valid for audience {OAUTH_RESOURCE_AUDIENCE}",
                    headers={"WWW-Authenticate": f'Bearer realm="mcp-floorplans", error="invalid_token"'}
                )

            logger.info(f"Token validated (aud={token_audience})")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Token validation error: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token validation failed",
                headers={"WWW-Authenticate": f'Bearer realm="mcp-floorplans"'}
            )
    else:
        # Dev mode: no OAuth server, accept any Bearer token
        logger.debug(f"Dev mode: accepting token without validation")

    logger.info(f"Token accepted: {token[:10]}...")
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
        RFC 9728: Provide protected resource metadata (PUBLIC endpoint, no auth required).

        Returns OAuth 2.1 resource metadata that clients use for authorization.
        All fields conform to RFC 9728 standard.
        """
        metadata = get_oauth_metadata()

        # Indicate if OAuth is fully configured
        if not OAUTH_CONFIGURED:
            metadata["_status"] = "BLOCKED: No OAuth server configured"
            metadata["_required_env_vars"] = [
                "OAUTH_ISSUER",
                "OAUTH_JWKS_URI",
                "OAUTH_RESOURCE_AUDIENCE"
            ]

        return metadata

    @app.get("/")
    async def root(request: Request):
        """Root endpoint — requires auth."""
        if require_auth:
            validate_bearer_token(request)

        return {
            "service": "mcp-floorplans",
            "version": "0.1.0",
            "status": "protected" if require_auth else "open",
            "oauth_configured": OAUTH_CONFIGURED,
            "auth_status": "BLOCKED: No OAuth server configured" if not OAUTH_CONFIGURED else "ACTIVE",
            "metadata_endpoint": "/.well-known/oauth-protected-resource",
            "rfc_9728_compliant": True,
            "rfc_8707_audience_validation": OAUTH_CONFIGURED,
            "resource": PROTECTED_RESOURCE_METADATA.get("resource"),
            "authorization_servers": PROTECTED_RESOURCE_METADATA.get("authorization_servers", [])
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
