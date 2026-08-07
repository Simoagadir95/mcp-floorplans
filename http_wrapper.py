"""
HTTP Wrapper for MCP Floorplans & Archviz Services with OAuth 2.1 Protection (RFC 9728)

Exposes MCP server functionality over HTTP with:
  - OAuth 2.1 Bearer token validation
  - RFC 9728 protected resource metadata endpoint
  - Space layout generation with authentication
  - MCP Streamable HTTP transport (JSON-RPC 2.0 over POST /mcp)
"""

import json
import logging
import os
from typing import Optional, Dict, Any
from datetime import datetime

from fastapi import FastAPI, HTTPException, status, Depends, Request, Body
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


# ============================================================================
# MCP STREAMABLE HTTP TRANSPORT (JSON-RPC 2.0 over POST)
# ============================================================================

class JsonRpcRequest(BaseModel):
    """JSON-RPC 2.0 request."""
    jsonrpc: str = "2.0"
    method: str
    params: Optional[Dict[str, Any]] = None
    id: Optional[Any] = None


class JsonRpcResponse(BaseModel):
    """JSON-RPC 2.0 response."""
    jsonrpc: str = "2.0"
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    id: Optional[Any] = None


@app.post("/mcp")
async def mcp_transport(
    req: JsonRpcRequest,
    request: Request,
    token: str = Depends(oauth_dependency)
) -> JsonRpcResponse:
    """
    MCP Streamable HTTP transport endpoint (JSON-RPC 2.0).

    Implements MCP protocol over HTTP POST with OAuth 2.1 token validation.
    Supports: initialize, tools/list, tools/call

    Args:
        req: JSON-RPC request
        token: Validated OAuth bearer token

    Returns:
        JSON-RPC response with result or error
    """
    logger.info(f"[{token[:10] if token else 'no-auth'}...] MCP request: {req.method}")

    try:
        method = req.method
        params = req.params or {}
        request_id = req.id

        # Route to MCP server methods
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "logging": {},
                    "tools": {}
                },
                "serverInfo": {
                    "name": "mcp-floorplans",
                    "version": "0.1.0"
                }
            }
            return JsonRpcResponse(result=result, id=request_id)

        elif method == "tools/list":
            # List available tools
            result = {
                "tools": [
                    {
                        "name": "generate_space_layouts",
                        "description": "Generate workspace layout variants from brief",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "surface_sqm": {
                                    "type": "number",
                                    "description": "Total workspace area in square meters"
                                },
                                "headcount": {
                                    "type": "integer",
                                    "description": "Number of people to accommodate"
                                },
                                "zone_types": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Desired zone types"
                                },
                                "collaboration_style": {
                                    "type": "string",
                                    "description": "high_collab | medium_collab | low_collab"
                                },
                                "project_id": {
                                    "type": "string",
                                    "description": "Project identifier"
                                }
                            },
                            "required": ["surface_sqm", "headcount", "zone_types"]
                        }
                    },
                    {
                        "name": "analyze_zone_adjacencies",
                        "description": "Analyze zone proximity and adjacency metrics",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "zones": {
                                    "type": "array",
                                    "description": "Zone definitions"
                                }
                            },
                            "required": ["zones"]
                        }
                    },
                    {
                        "name": "validate_space_brief",
                        "description": "Validate a workspace brief for consistency",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "surface_sqm": {"type": "number"},
                                "headcount": {"type": "integer"},
                                "zone_types": {
                                    "type": "array",
                                    "items": {"type": "string"}
                                }
                            },
                            "required": ["surface_sqm", "headcount", "zone_types"]
                        }
                    }
                ]
            }
            return JsonRpcResponse(result=result, id=request_id)

        elif method == "tools/call":
            # Call a tool (generate_space_layouts, etc.)
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})

            if not tool_name:
                return JsonRpcResponse(
                    error={"code": -32602, "message": "Missing tool name"},
                    id=request_id
                )

            try:
                # Route tool calls
                if tool_name == "generate_space_layouts":
                    try:
                        layout_json = generate_space_layouts_json(
                            surface_sqm=float(tool_args.get("surface_sqm", 0)),
                            headcount=int(tool_args.get("headcount", 0)),
                            zone_types=tool_args.get("zone_types", ["open-space"]),
                            project_id=tool_args.get("project_id", "default")
                        )
                        result = {"content": [{"type": "text", "text": layout_json}], "isError": False}
                        return JsonRpcResponse(result=result, id=request_id)
                    except Exception as e:
                        return JsonRpcResponse(
                            error={"code": -32603, "message": str(e)},
                            id=request_id
                        )
                else:
                    return JsonRpcResponse(
                        error={"code": -32601, "message": f"Tool not implemented: {tool_name}"},
                        id=request_id
                    )
            except Exception as e:
                return JsonRpcResponse(
                    error={"code": -32603, "message": str(e)},
                    id=request_id
                )

        else:
            # Unknown method
            return JsonRpcResponse(
                error={"code": -32601, "message": f"Unknown method: {method}"},
                id=request_id
            )

    except Exception as e:
        logger.error(f"MCP transport error: {e}")
        return JsonRpcResponse(
            error={"code": -32603, "message": f"Internal error: {str(e)}"},
            id=req.id
        )


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8851"))
    uvicorn.run(app, host="0.0.0.0", port=port)
