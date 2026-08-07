"""
HTTP wrapper for mcp-floorplans MCP server with OAuth 2.1 (D3).

Provides FastAPI HTTP endpoints for workspace layout generation.
All tool endpoints require OAuth 2.1 Bearer token (except public metadata).
"""

import uvicorn
import logging
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from space_calculator import generate_space_layouts_json
from oauth_middleware import add_oauth_routes, oauth_dependency, PROTECTED_RESOURCE_METADATA

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="mcp-floorplans HTTP API",
    version="0.2.0",
    description="Workspace layout generation with OAuth 2.1 protection"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add OAuth 2.1 routes (/.well-known/oauth-protected-resource, /health, /)
add_oauth_routes(app, require_auth=True)


# Request/Response models
class GenerateLayoutRequest(BaseModel):
    surface_sqm: float
    headcount: int
    zone_types: list[str]
    collaboration_style: Optional[str] = "medium_collab"
    project_id: Optional[str] = "default"


class HealthResponse(BaseModel):
    status: str
    service: str
    auth_required: bool
    auth_scheme: str


@app.get("/health", response_model=HealthResponse)
async def health():
    """Health check endpoint (public, no auth required)."""
    return {
        "status": "ok",
        "service": "mcp-floorplans",
        "auth_required": True,
        "auth_scheme": "Bearer (OAuth 2.1)"
    }


@app.post("/layouts/generate")
async def generate_layouts(
    request: GenerateLayoutRequest,
    token: str = Depends(oauth_dependency)
):
    """
    Generate workspace layout variants.

    Requires OAuth 2.1 Bearer token with 'space:write' scope.

    Args:
        surface_sqm: Total workspace area in m²
        headcount: Number of occupants
        zone_types: List of desired zone types
        collaboration_style: "high_collab", "medium_collab", or "low_collab"
        project_id: Project identifier

    Returns:
        JSON with 3 layout variants, each containing zones and metrics
    """
    try:
        # Validation
        if request.surface_sqm <= 0:
            raise HTTPException(status_code=400, detail="surface_sqm must be > 0")
        if request.headcount <= 0:
            raise HTTPException(status_code=400, detail="headcount must be > 0")
        if not request.zone_types:
            raise HTTPException(status_code=400, detail="zone_types list cannot be empty")

        logger.info(f"Generating layouts for project {request.project_id}: "
                   f"{request.surface_sqm}m², {request.headcount} people")

        # Generate layouts
        layout_json = generate_space_layouts_json(
            surface_sqm=request.surface_sqm,
            headcount=request.headcount,
            zone_types=request.zone_types,
            project_id=request.project_id
        )

        result = {
            "project_id": request.project_id,
            "status": "success",
            "message": "3 layout variants generated",
            "layout_data": eval(layout_json)  # Parse JSON to dict
        }

        logger.info(f"✓ Layouts generated: {request.project_id}")
        return result

    except ValueError as e:
        logger.error(f"Validation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Generation failed: {e}")
        raise HTTPException(status_code=500, detail=f"Layout generation failed: {str(e)}")


@app.get("/layouts/{project_id}")
async def get_layout(
    project_id: str,
    token: str = Depends(oauth_dependency)
):
    """
    Retrieve cached layout for a project.

    Requires OAuth 2.1 Bearer token with 'space:read' scope.

    Note: In current implementation, layouts are generated on-demand.
    Phase D3.2 will add caching with database backend.
    """
    raise HTTPException(
        status_code=501,
        detail="Layout caching not yet implemented. Use POST /layouts/generate"
    )


@app.get("/info")
async def info():
    """Service information (public)."""
    return {
        "service": "mcp-floorplans",
        "version": "0.2.0",
        "status": "active",
        "auth": {
            "required": True,
            "scheme": "OAuth 2.1 Bearer",
            "resource_uri": "urn:virtus:mcp-floorplans",
            "metadata_endpoint": "/.well-known/oauth-protected-resource",
            "scopes": ["space:read", "space:write"]
        },
        "endpoints": {
            "health": "GET /health",
            "metadata": "GET /.well-known/oauth-protected-resource",
            "generate": "POST /layouts/generate (requires Bearer token)",
            "info": "GET /info"
        }
    }


if __name__ == "__main__":
    logger.info("Starting mcp-floorplans HTTP server on 0.0.0.0:8820")
    logger.info("Auth required: Yes (OAuth 2.1 Bearer)")
    logger.info("Public endpoints: /health, /.well-known/oauth-protected-resource, /info, /")
    uvicorn.run(app, host="0.0.0.0", port=8820)
