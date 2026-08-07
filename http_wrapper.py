"""
HTTP Wrapper for MCP Floorplans & Archviz Services with OAuth 2.1 Protection (RFC 9728)

Exposes MCP server functionality over HTTP with:
  - OAuth 2.1 Bearer token validation
  - RFC 9728 protected resource metadata endpoint
  - Space layout generation with authentication
"""

import json
import logging
import os
from typing import Optional, Dict, Any
from datetime import datetime

from fastapi import FastAPI, HTTPException, status, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from oauth_middleware import add_oauth_routes, oauth_dependency, OAuthMiddleware
from space_calculator import generate_space_layouts_json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================================
# REQUEST/RESPONSE MODELS
# ============================================================================


class LayoutRequest(BaseModel):
    """Request to generate space layouts."""
    surface_sqm: float
    headcount: int
    zone_types: list[str]
    project_id: Optional[str] = None
    collaboration_style: Optional[str] = "medium_collab"


class LayoutResponse(BaseModel):
    """Response with space layout variants."""
    project_id: str
    brief: Dict[str, Any]
    variants: list[Dict[str, Any]]


# ============================================================================
# APPLICATION SETUP
# ============================================================================

app = FastAPI(
    title="MCP Floorplans & Archviz — OAuth Protected Wrapper",
    version="0.1.0",
    description="HTTP wrapper for MCP space layout services with OAuth 2.1 protection"
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# OAuth middleware (protects non-public paths)
require_auth = os.getenv("REQUIRE_OAUTH", "true").lower() == "true"
app.add_middleware(OAuthMiddleware, require_auth=require_auth)

# Add OAuth routes (.well-known/oauth-protected-resource, etc)
add_oauth_routes(app, require_auth=require_auth)

# ============================================================================
# PROTECTED ENDPOINTS
# ============================================================================


@app.post("/generate-layouts", response_model=LayoutResponse)
async def generate_layouts(
    req: LayoutRequest,
    token: str = Depends(oauth_dependency)
) -> LayoutResponse:
    """
    Generate space layout variants from workspace brief.

    Requires OAuth 2.1 Bearer token with 'space:write' scope.

    Args:
        req: Layout generation request (surface_sqm, headcount, zone_types)
        token: Validated OAuth bearer token (injected via dependency)

    Returns:
        Layout variants with zone details and metrics
    """
    logger.info(f"[{token[:10]}...] Generating layouts: {req.surface_sqm}sqm, {req.headcount} people")

    try:
        layout_json = generate_space_layouts_json(
            surface_sqm=req.surface_sqm,
            headcount=req.headcount,
            zone_types=req.zone_types,
            project_id=req.project_id or "anonymous"
        )
        layout_data = json.loads(layout_json)

        return LayoutResponse(
            project_id=layout_data.get("project_id", "anonymous"),
            brief=layout_data.get("brief", {}),
            variants=layout_data.get("variants", [])
        )
    except Exception as e:
        logger.error(f"Layout generation failed: {e}")
        raise HTTPException(status_code=500, detail=f"Layout generation failed: {str(e)}")


@app.get("/layouts/{project_id}", response_model=LayoutResponse)
async def get_layout(
    project_id: str,
    surface_sqm: float = 400.0,
    headcount: int = 40,
    zone_types: str = "open-space,meeting,quiet-zone,phone-booth,break-room",
    token: str = Depends(oauth_dependency)
) -> LayoutResponse:
    """
    Get space layout for a project (convenience GET endpoint).

    Requires OAuth 2.1 Bearer token with 'space:read' scope.

    Query Parameters:
        surface_sqm: Total workspace area (default: 400)
        headcount: Number of people (default: 40)
        zone_types: Comma-separated zone types
        token: Bearer token (in Authorization header)

    Returns:
        Layout variants
    """
    logger.info(f"[{token[:10]}...] Fetching layout {project_id}")

    try:
        zone_list = [z.strip() for z in zone_types.split(",")]
        layout_json = generate_space_layouts_json(
            surface_sqm=surface_sqm,
            headcount=headcount,
            zone_types=zone_list,
            project_id=project_id
        )
        layout_data = json.loads(layout_json)

        return LayoutResponse(
            project_id=project_id,
            brief=layout_data.get("brief", {}),
            variants=layout_data.get("variants", [])
        )
    except Exception as e:
        logger.error(f"Layout fetch failed: {e}")
        raise HTTPException(status_code=500, detail=f"Layout fetch failed: {str(e)}")


@app.get("/health")
async def health_check() -> Dict[str, Any]:
    """
    Health check endpoint (public, no auth required).

    Returns:
        Service status
    """
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "service": "mcp-floorplans-wrapper",
        "version": "0.1.0",
        "oauth_required": require_auth
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8851"))
    uvicorn.run(app, host="0.0.0.0", port=port)
