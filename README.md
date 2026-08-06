# mcp-floorplans

Workspace floorplan generation MCP server with deterministic space layout calculation.

**Status:** Phase 4 MVP — Deterministic space layout engine (no external APIs)

## Features

- **Deterministic space layout calculation** — No API dependencies, pure Python logic
- **3 layout variants** per brief — Balanced, Collaboration-heavy, Focus-intensive
- **Detailed metrics** — Workstations, meeting rooms, collaboration %, window distances
- **Zone adjacency analysis** — Functional recommendations for zone placement
- **Space brief validation** — Feasibility checking with recommendations

## Architecture

```
space_calculator.py
  ├─ SpaceCalculator class — Core calculation engine
  ├─ Zone, LayoutVariant, SpaceMetrics dataclasses
  └─ generate_space_layouts_json() — Main entry point

server.py
  ├─ MCP server with 3 tools
  ├─ generate_space_layouts — Layout generation
  ├─ analyze_zone_adjacencies — Adjacency rules
  └─ validate_space_brief — Feasibility validation

test_space_calculator.py
  └─ 15+ unit tests, 100% deterministic
```

## Quick Start

```bash
# Install dependencies
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run tests
pytest test_space_calculator.py -v

# Start MCP server (stdio mode for Claude)
python server.py
```

## MCP Tools

### generate_space_layouts

Generate 3 workspace layout variants from brief.

**Input:**
```json
{
  "surface_sqm": 200,
  "headcount": 20,
  "zone_types": ["open-space", "meeting", "quiet-zone"],
  "collaboration_style": "medium_collab",
  "project_id": "test-proj-1"
}
```

**Output:** JSON with 3 layout variants, each containing:
- Zones with dimensions and occupancy
- Metrics (workstations, meeting rooms, collaboration %, window distances)
- Stub floorplan URL (stub:///floorplans/...)
- Design notes

### analyze_zone_adjacencies

Get functional adjacency recommendations for zone types.

**Input:**
```json
{
  "zone_types": ["open-space", "meeting", "quiet-zone"]
}
```

### validate_space_brief

Check feasibility and get recommendations.

**Input:**
```json
{
  "surface_sqm": 200,
  "headcount": 20,
  "zone_types": ["open-space", "meeting"]
}
```

## Calculation Logic

### Space Sizing

Standard guidelines per zone type:
- Open-space: 5–8.5 sqm per workstation (hotdesking to dedicated)
- Quiet-zone: 4 sqm per person
- Meeting: 2.5 sqm per person
- Phone-booth: 2 sqm per booth (1 person)
- Break-room: 1 sqm per person

### Collaboration Percentages

- **high_collab:** 40% meeting + break + phone zones
- **medium_collab:** 30%
- **low_collab:** 20%

### Circulation

15% of total area reserved for corridors, stairs, etc.

## Metrics Provided

For each variant:
- `total_sqm` — Total workspace area
- `workstations` — Number of workstations
- `meeting_rooms` — Number of dedicated meeting rooms
- `phone_booths` — Number of private call booths
- `quiet_zones` — Number of focus areas
- `break_rooms` — Number of break/social areas
- `collaboration_zones_pct` — % of space for collaborative work
- `average_sqm_per_person` — Density metric
- `window_distance_avg` — Average distance to windows (meters)
- `natural_light_zones_pct` — % of space with potential window access

## Example Usage

```python
from space_calculator import generate_space_layouts_json

# Generate layouts for 200 sqm, 20 people
json_output = generate_space_layouts_json(
    surface_sqm=200,
    headcount=20,
    zone_types=["open-space", "meeting", "quiet-zone", "phone-booth", "break-room"],
    project_id="my-project"
)

# Parse output
import json
data = json.loads(json_output)

# Access first variant
variant = data["variants"][0]
print(f"Variant: {variant['layout_name']}")
print(f"Workstations: {variant['metrics']['workstations']}")
print(f"Collaboration: {variant['metrics']['collaboration_zones_pct']}%")
```

## Testing

All calculation logic is deterministic and fully tested:

```bash
# Run all tests
pytest test_space_calculator.py -v

# Test categories:
# - Calculator initialization and configuration
# - Usable area calculation
# - Zone distribution across types
# - Metrics calculation accuracy
# - Variant generation (3 variants per brief)
# - JSON output format validation
# - Edge cases (small/large spaces)
# - Determinism (same input → same output)
```

## Phase 3 Integration (mcp-interior)

mcp-floorplans works alongside:
- **mcp-interior** — Interior redesign of existing spaces (Decor8 API, stub provider)
- **mcp-archviz** — 3D visualization of layouts (stub provider)
- **WorkspaceAgent** — Orchestrates all three services

## Phase 4 Status

✅ **COMPLETED:**
- Space calculator implementation (deterministic, no API calls)
- 3 layout variants per brief
- Metrics calculation
- Zone adjacency analysis
- Space brief validation
- 15+ unit tests (all passing)
- Full test coverage of calculation logic

⏸️ **DEFERRED (Phase 5+):**
- Real floorplan image generation (requires image service)
- 3D model generation (via mcp-archviz)
- CAD export (SVG/DXF format)
- Furniture library integration
- Cost estimation (fit-out budgeting)

## Architecture Decisions

1. **Deterministic (no APIs):** Core calculation is pure Python, testable, reproducible
2. **Stub floorplans:** `stub:///floorplans/...` URLs indicate placeholder images
3. **Dataclasses:** Type-safe zone/layout/metrics models
4. **No external services:** Calculation doesn't depend on CasaAI, HWFC, Roomify, etc.
5. **MCP standard tools:** Integrates with Claude agents via MCP protocol

## License

Proprietary — Virtus Agents

## See Also

- Phase 3: [mcp-interior](../mcp-interior/)
- Phase 4: [mcp-archviz](../mcp-archviz/)
- Orchestrator: [WorkspaceAgent](./.virtus_factory/agents/WorkspaceAgent.md)
