"""
mcp-floorplans — MCP server for workspace floorplan generation.

Provides tools for generating deterministic space layouts from workspace briefs.
No external API calls - pure computational logic.
"""

import json
import sys
from typing import Any

from mcp.server import Server
from mcp.types import (
    Tool,
    TextContent,
    ToolResponse,
)

from space_calculator import generate_space_layouts_json, SpaceCalculator

# Initialize MCP server
server = Server("mcp-floorplans")


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> ToolResponse:
    """Handle tool calls."""
    if name == "generate_space_layouts":
        return await generate_space_layouts_tool(arguments)
    elif name == "analyze_zone_adjacencies":
        return await analyze_zone_adjacencies_tool(arguments)
    elif name == "validate_space_brief":
        return await validate_space_brief_tool(arguments)
    else:
        raise ValueError(f"Unknown tool: {name}")


async def generate_space_layouts_tool(arguments: dict) -> ToolResponse:
    """
    Generate workspace layout variants from brief.

    Input:
    - surface_sqm (float): Total workspace area in square meters
    - headcount (int): Number of people to accommodate
    - zone_types (list[str]): Desired zone types
      ["open-space", "meeting", "quiet-zone", "phone-booth", "break-room"]
    - collaboration_style (str, optional): "high_collab" | "medium_collab" | "low_collab"
    - project_id (str, optional): Project identifier

    Output:
    - JSON with 3 layout variants, each containing zones and calculated metrics
    """
    try:
        surface_sqm = float(arguments.get("surface_sqm", 0))
        headcount = int(arguments.get("headcount", 0))
        zone_types = arguments.get("zone_types", ["open-space", "meeting", "quiet-zone"])
        collaboration_style = arguments.get("collaboration_style", "medium_collab")
        project_id = arguments.get("project_id", "default")

        # Validation
        if surface_sqm <= 0:
            return ToolResponse(
                content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "status": "error",
                        "message": "surface_sqm must be > 0",
                        "code": "INVALID_SURFACE"
                    })
                )],
                is_error=True
            )

        if headcount <= 0:
            return ToolResponse(
                content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "status": "error",
                        "message": "headcount must be > 0",
                        "code": "INVALID_HEADCOUNT"
                    })
                )],
                is_error=True
            )

        if collaboration_style not in ["high_collab", "medium_collab", "low_collab"]:
            collaboration_style = "medium_collab"

        # Generate layouts
        layouts_json = generate_space_layouts_json(
            surface_sqm=surface_sqm,
            headcount=headcount,
            zone_types=zone_types,
            project_id=project_id
        )

        return ToolResponse(
            content=[TextContent(
                type="text",
                text=layouts_json
            )]
        )

    except Exception as e:
        return ToolResponse(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "status": "error",
                    "message": str(e),
                    "code": "CALCULATION_ERROR"
                })
            )],
            is_error=True
        )


async def analyze_zone_adjacencies_tool(arguments: dict) -> ToolResponse:
    """
    Analyze and suggest zone adjacencies based on functional requirements.

    Input:
    - zone_types (list[str]): Zone types in the design

    Output:
    - Adjacency recommendations (which zones should be near each other)
    """
    try:
        zone_types = arguments.get("zone_types", [])

        adjacency_rules = {
            "open-space": ["meeting", "phone-booth", "circulation"],
            "meeting": ["open-space", "circulation", "quiet-zone"],
            "quiet-zone": ["circulation", "break-room"],
            "phone-booth": ["open-space", "circulation"],
            "break-room": ["quiet-zone", "circulation"],
            "storage": ["circulation"],
        }

        recommendations = {}
        for zone in zone_types:
            if zone in adjacency_rules:
                recommendations[zone] = {
                    "should_be_near": adjacency_rules[zone],
                    "reasoning": get_adjacency_reasoning(zone)
                }

        return ToolResponse(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "status": "success",
                    "adjacencies": recommendations
                }, indent=2)
            )]
        )

    except Exception as e:
        return ToolResponse(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "status": "error",
                    "message": str(e),
                    "code": "ANALYSIS_ERROR"
                })
            )],
            is_error=True
        )


async def validate_space_brief_tool(arguments: dict) -> ToolResponse:
    """
    Validate a workspace brief for feasibility.

    Input:
    - surface_sqm (float): Total workspace area
    - headcount (int): Number of people
    - zone_types (list[str]): Desired zones

    Output:
    - Validation status, warnings, and recommendations
    """
    try:
        surface_sqm = float(arguments.get("surface_sqm", 0))
        headcount = int(arguments.get("headcount", 0))
        zone_types = arguments.get("zone_types", [])

        warnings = []
        recommendations = []

        # Check space per person (standard: 8-12 sqm per person)
        sqm_per_person = surface_sqm / headcount if headcount > 0 else 0

        if sqm_per_person < 5:
            warnings.append(
                f"Space is tight: {sqm_per_person:.1f} sqm/person (ideal: 8-12)"
            )
            recommendations.append("Consider hot-desking or shift-based work")
        elif sqm_per_person > 15:
            recommendations.append(
                f"Space is generous: {sqm_per_person:.1f} sqm/person - can accommodate all zone types"
            )

        # Check zone type feasibility
        required_sqm = {
            "meeting": 12,  # min for 1 meeting room
            "quiet-zone": 20,  # min for quiet area
            "phone-booth": 4,  # min for 2 booths
            "break-room": 10,  # min for small break area
            "storage": 15,  # min storage
        }

        usable_area = surface_sqm * 0.85  # 85% usable (15% circulation)

        for zone_type, min_sqm in required_sqm.items():
            if zone_type in zone_types and usable_area < min_sqm:
                warnings.append(
                    f"Space too small for {zone_type}: need {min_sqm} sqm, have {usable_area:.0f}"
                )

        return ToolResponse(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "status": "success",
                    "valid": len(warnings) == 0,
                    "sqm_per_person": sqm_per_person,
                    "warnings": warnings,
                    "recommendations": recommendations
                }, indent=2)
            )]
        )

    except Exception as e:
        return ToolResponse(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "status": "error",
                    "message": str(e),
                    "code": "VALIDATION_ERROR"
                })
            )],
            is_error=True
        )


def get_adjacency_reasoning(zone_type: str) -> str:
    """Get reasoning for adjacency recommendations."""
    reasons = {
        "open-space": "Needs access to collaboration spaces and natural flow",
        "meeting": "Should be accessible from open space but with sound isolation",
        "quiet-zone": "Needs minimal traffic; adjacent to break areas for movement",
        "phone-booth": "Direct access to open space for visibility and confidence",
        "break-room": "Should provide mental break from focus work",
        "storage": "Central location for easy access but away from main work areas",
    }
    return reasons.get(zone_type, "")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        Tool(
            name="generate_space_layouts",
            description="Generate workspace layout variants from brief (surface, headcount, zone types). Returns 3 deterministic layout variants with calculated metrics.",
            inputSchema={
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
                        "description": 'Zone types to include: ["open-space", "meeting", "quiet-zone", "phone-booth", "break-room", "storage"]'
                    },
                    "collaboration_style": {
                        "type": "string",
                        "enum": ["high_collab", "medium_collab", "low_collab"],
                        "description": "Collaboration intensity level"
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Project identifier"
                    }
                },
                "required": ["surface_sqm", "headcount", "zone_types"]
            }
        ),
        Tool(
            name="analyze_zone_adjacencies",
            description="Analyze zone adjacency recommendations for optimal layout flow.",
            inputSchema={
                "type": "object",
                "properties": {
                    "zone_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Zone types to analyze"
                    }
                },
                "required": ["zone_types"]
            }
        ),
        Tool(
            name="validate_space_brief",
            description="Validate workspace brief for feasibility and provide recommendations.",
            inputSchema={
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
                    }
                },
                "required": ["surface_sqm", "headcount"]
            }
        )
    ]


if __name__ == "__main__":
    # Run MCP server
    import asyncio

    async def main():
        async with server:
            # For stdio transport, we don't need to do anything here
            # The server will be driven by stdin/stdout
            pass

    # Use the standard async run pattern for MCP
    try:
        import mcp.server.stdio
        mcp.server.stdio.stdio_server(server)
    except ImportError:
        # Fallback: simple stdio loop
        print("mcp-floorplans server running", file=sys.stderr)
