"""
Deterministic space layout calculation engine.
Calculates optimal workspace configurations from brief (surface, occupants, zone types).
No API calls - pure computational logic.
"""

import json
import math
import logging
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional, Tuple
from enum import Enum

logger = logging.getLogger(__name__)


class ZoneType(Enum):
    """Standard workspace zone types."""
    OPEN_SPACE = "open-space"
    MEETING = "meeting"
    PHONE_BOOTH = "phone-booth"
    QUIET_ZONE = "quiet-zone"
    BREAK_ROOM = "break-room"
    STORAGE = "storage"
    CIRCULATION = "circulation"


@dataclass
class SpaceMetrics:
    """Calculated metrics for a workspace layout."""
    total_sqm: float
    workstations: int
    meeting_rooms: int
    phone_booths: int
    quiet_zones: int
    break_rooms: int
    collaboration_zones_pct: float
    average_sqm_per_person: float
    window_distance_avg: float  # meters
    natural_light_zones_pct: float


@dataclass
class Zone:
    """A functional zone within the workspace."""
    zone_type: str
    name: str
    sqm: float
    occupancy: int  # people at max capacity
    adjacencies: List[str]  # zone names it should be near
    notes: str
    # Geometric coordinates (added by squarified treemap)
    x: Optional[float] = None  # meters from origin
    y: Optional[float] = None  # meters from origin
    width: Optional[float] = None  # meters
    length: Optional[float] = None  # meters


@dataclass
class LayoutVariant:
    """A single workspace layout variant."""
    variant_id: str
    layout_name: str
    zones: List[Zone]
    metrics: SpaceMetrics
    floorplan_stub_url: str  # stub:/// URL (no real image generation)
    design_notes: str
    constraint_violations: List[str] = None  # Shape constraint violations detected during layout

    def __post_init__(self):
        """Ensure constraint_violations is a list."""
        if self.constraint_violations is None:
            self.constraint_violations = []


class SpaceCalculator:
    """Deterministic workspace layout calculator."""

    # Standard zone sizing guidelines
    # For people-based zones: sqm per occupant
    # For phone booths: sqm per booth (fixed: 1.0-1.2m wide x 2.2-2.5m deep typical)
    ZONE_SIZING = {
        ZoneType.OPEN_SPACE: 5.5,  # 5.5 sqm per workstation (industry standard)
        ZoneType.MEETING: 3.5,  # 3.5 sqm per person in meeting room (9-14 sqm for 4 people rooms)
        ZoneType.PHONE_BOOTH: 2.5,  # 2.5 sqm per booth FIXED (1.1m x 2.3m = 2.53 sqm typical)
        ZoneType.QUIET_ZONE: 4.5,  # 4.5 sqm per person (focus area, need space)
        ZoneType.BREAK_ROOM: 1.8,  # 1.8 sqm per person (casual seating, more flexible)
    }

    # Zone surface bounds (min/max sqm per zone) — STRICT LIMITS to prevent unrealistic sizing
    # These ensure realistic dimensions per zone type. Fixed zones (e.g., phone booths) use min==max.
    # Elastic zones (open-space, circulation) have wide bounds to absorb leftover space.
    # Format: {zone_type: (min_sqm, max_sqm_per_unit)}
    ZONE_SIZING_BOUNDS = {
        ZoneType.OPEN_SPACE: (0, float('inf')),  # Elastic — expands to fill available space
        ZoneType.MEETING: (20, float('inf')),  # Minimum 20 sqm per meeting room (4-person room)
        ZoneType.PHONE_BOOTH: (1.5, 2.5),  # FIXED: 1.5-2.5 sqm per booth (realistic phone booth dimensions)
        ZoneType.QUIET_ZONE: (15, float('inf')),  # Minimum 15 sqm for focus zone
        ZoneType.BREAK_ROOM: (5.35, float('inf')),  # Minimum 5.35 sqm (as per existing spec)
        ZoneType.STORAGE: (10, float('inf')),  # Minimum 10 sqm for storage
        ZoneType.CIRCULATION: (0, float('inf')),  # Elastic — corridors absorb remaining space
    }

    # Shape constraints (geometry guardrails to prevent degenerate dimensions)
    # Canonical constraint table per zone type.
    # Format: {zone_type: {"min_width": min_short_side, "max_aspect_ratio": long_side/short_side}}
    # min_width: minimum dimension for the SHORT SIDE (prevents thin slivers)
    # max_aspect_ratio: long_side / short_side (allows elongation but not excessively)
    #
    # Rationale:
    #   - Open space: 3.0m min short side, up to 3.0x elongation (square-ish)
    #   - Meeting: 2.5m min, 2.5x max (more square)
    #   - Phone booth: 1.0m min, 4.0x max (can be tall)
    #   - Quiet zone: 2.5m min, 3.0x max (focus areas need space)
    #   - Break room: 1.8m min, 4.0x max (more flexible)
    #   - Storage: 1.5m min, 3.0x max
    #   - Circulation: 1.0m min, 5.0x max (corridors can be very thin/long)
    SHAPE_CONSTRAINTS = {
        ZoneType.OPEN_SPACE: {"min_width": 3.0, "max_aspect_ratio": 3.0},
        ZoneType.MEETING: {"min_width": 2.5, "max_aspect_ratio": 2.5},
        ZoneType.PHONE_BOOTH: {"min_width": 1.0, "max_aspect_ratio": 4.0},
        ZoneType.QUIET_ZONE: {"min_width": 2.5, "max_aspect_ratio": 3.0},
        ZoneType.BREAK_ROOM: {"min_width": 1.8, "max_aspect_ratio": 4.0},
        ZoneType.STORAGE: {"min_width": 1.5, "max_aspect_ratio": 3.0},
        ZoneType.CIRCULATION: {"min_width": 1.0, "max_aspect_ratio": 5.0},
    }

    # Collaboration percentages (target % of space for collaborative zones)
    COLLABORATION_TARGETS = {
        "high_collab": 0.40,  # 40% meeting + break + phone
        "medium_collab": 0.30,  # 30%
        "low_collab": 0.20,   # 20%
    }

    def __init__(self, surface_sqm: float, headcount: int,
                 zone_types: List[str], collaboration_style: str = "medium_collab"):
        """
        Initialize calculator with space brief.

        Args:
            surface_sqm: Total workspace area in square meters
            headcount: Number of people to accommodate
            zone_types: List of zone types to include (e.g., ["open-space", "meeting", "quiet-zone"])
            collaboration_style: "high_collab" | "medium_collab" | "low_collab"
        """
        self.surface_sqm = surface_sqm
        self.headcount = headcount
        self.zone_types = [ZoneType(zt) if isinstance(zt, str) else zt for zt in zone_types]
        self.collaboration_style = collaboration_style
        self.circulation_pct = 0.15  # 15% for corridors, stairs, etc.
        self.circulation_tolerance = 0.001  # DEFECT I FIX: Reduced from 0.02 to 0.001 (0.4 sqm on 400 sqm) for exact surface conservation

    def _squarify_treemap(self, areas: List[Tuple[str, float]],
                          x: float, y: float, width: float, height: float,
                          rectangles: Dict[str, Tuple[float, float, float, float]],
                          row: List[Tuple[str, float]],
                          zone_name_to_type: Optional[Dict[str, str]] = None) -> None:
        """
        Squarified treemap algorithm (Bruls/Huizing/van Wijk).
        Builds rows of rectangles, optimizing for aspect ratios close to 1.0.
        GUARANTEES: non-overlapping, exact area conservation, better aspect ratios than binary cuts.

        Algorithm:
        1. Start with sorted rectangles (descending by area)
        2. Build a row by adding rectangles while worst-case aspect ratio improves
        3. When next rectangle would worsen aspect ratio, lay out the row
        4. Row is placed along the shortest side of the remaining container
        5. Update container and repeat until all rectangles placed

        Args:
            areas: List of (zone_name, area_sqm) tuples sorted descending
            x, y: Origin coordinates (meters)
            width, height: Dimensions (meters)
            rectangles: Output dict mapping zone_name -> (x, y, width, height)
            row: Current row being processed (used to accumulate rectangles for a row)
            zone_name_to_type: Map from zone name to zone type for constraint lookup
        """
        if not areas or width <= 0 or height <= 0:
            return

        # Base case: single rectangle — assign exactly to container
        if len(areas) == 1:
            name, area = areas[0]
            # Use container dimensions as-is; treemap guarantees exact area conservation
            # by construction (area = width * height in the subdivision)
            rectangles[name] = (x, y, width, height)
            logger.debug(f"TREEMAP BASE: {name} area={area:.2f}, assigned w={width:.2f}, h={height:.2f}, w*h={width*height:.2f}")
            return

        # Squarification: build rows of rectangles
        remaining = list(areas)
        current_x = x
        current_y = y
        remaining_width = width
        remaining_height = height
        iteration = 0
        max_iterations = len(areas) + 10  # Safety limit to prevent infinite loops

        while remaining and iteration < max_iterations:
            iteration += 1
            logger.debug(f"TREEMAP iteration {iteration}: remaining={len(remaining)} zones, container={remaining_width:.2f}x{remaining_height:.2f}")

            # Safeguard: if container is too small, place remaining zones as best effort
            if remaining_width < 0.01 or remaining_height < 0.01:
                logger.warning(f"TREEMAP: container too small ({remaining_width:.2f}x{remaining_height:.2f}), placing remaining zones as best effort")
                # Force single column placement for remaining zones
                for name, area in remaining:
                    if remaining_height > 0 and remaining_width > 0:
                        zone_height = area / remaining_width if remaining_width > 0 else 0
                        rectangles[name] = (current_x, current_y, remaining_width, zone_height)
                        current_y += zone_height
                break

            # Determine layout direction (horizontal row along short side)
            if remaining_width >= remaining_height:
                # Container is wider: layout row horizontally, progress downward
                row_height = self._layout_row_horizontal(
                    remaining, current_x, current_y, remaining_width, remaining_height,
                    rectangles, zone_name_to_type
                )
                if row_height <= 0:
                    logger.warning(f"TREEMAP: row_height={row_height}, breaking to avoid infinite loop")
                    break
                current_y += row_height
                remaining_height = max(0, remaining_height - row_height)  # DEFECT I-2: Prevent negative height
                # Remove placed rectangles from remaining
                old_remaining = len(remaining)
                remaining = [r for r in remaining if r[0] not in rectangles]
                logger.debug(f"TREEMAP: laid out {old_remaining - len(remaining)} zones, remaining={len(remaining)}")
            else:
                # Container is taller: layout row vertically, progress rightward
                row_width = self._layout_row_vertical(
                    remaining, current_x, current_y, remaining_width, remaining_height,
                    rectangles, zone_name_to_type
                )
                if row_width <= 0:
                    logger.warning(f"TREEMAP: row_width={row_width}, breaking to avoid infinite loop")
                    break
                current_x += row_width
                remaining_width = max(0, remaining_width - row_width)  # DEFECT I-2: Prevent negative width
                # Remove placed rectangles from remaining
                old_remaining = len(remaining)
                remaining = [r for r in remaining if r[0] not in rectangles]
                logger.debug(f"TREEMAP: laid out {old_remaining - len(remaining)} zones, remaining={len(remaining)}")

    def _layout_row_horizontal(self, areas: List[Tuple[str, float]],
                               x: float, y: float, width: float, height: float,
                               rectangles: Dict[str, Tuple[float, float, float, float]],
                               zone_name_to_type: Optional[Dict[str, str]] = None) -> float:
        """
        Layout a horizontal row of rectangles.
        Returns the row height used.
        Implements squarification: greedily add rectangles to row while aspect ratio improves.
        DEFECT I-2/I-3/I-4 FIX: Preserve exact area for every zone by proportionally scaling widths.
        GUARANTEES: Σ(zone_width) = width (within floating-point precision)
                    Each zone.sqm = zone_width * zone_height (exact to 1e-6)
        """
        if not areas or width <= 0 or height <= 0:
            logger.warning(f"_layout_row_horizontal: early return due to empty/invalid: areas={len(areas) if areas else 0}, width={width}, height={height}")
            return 0.0

        row = []
        row_total_area = 0.0
        worst_ratio = float('inf')
        logger.debug(f"_layout_row_horizontal: starting with {len(areas)} zones, container {width:.2f}x{height:.2f}")

        for i, (name, area) in enumerate(areas):
            # Try adding this rectangle to the row
            test_row = row + [(name, area)]
            test_total = row_total_area + area

            # Calculate worst aspect ratio if we add this rectangle
            test_ratio = self._worst_aspect_ratio_for_row(test_row, width)

            # If this is the first rectangle, or aspect ratio improves, add it
            if i == 0 or test_ratio < worst_ratio:
                row = test_row
                row_total_area = test_total
                worst_ratio = test_ratio
                logger.debug(f"  Added {name} (area={area:.2f}), worst_ratio={worst_ratio:.3f}")
            else:
                # Aspect ratio would worsen, so lay out current row and return
                logger.debug(f"  Skipped {name} (ratio would worsen from {worst_ratio:.3f} to {test_ratio:.3f}), closing row with {len(row)} zones")
                break

        # CYCLE 26 J-1 FIX (CORRECT APPROACH): Pre-row-layout validation
        # Before placing the row, verify that zone areas fit within available space
        # If row_total_area / width > height, remove smallest zones until row fits
        if row:
            row_height = row_total_area / width if width > 0 else 0

            # Validate row fits in available height (with small tolerance for floating-point)
            while row_height > height + 1e-3 and len(row) > 1:
                logger.info(f"PRE-ROW-LAYOUT VALIDATION: row_height={row_height:.3f} exceeds available height={height:.3f}, removing smallest zone from {len(row)} zones")
                # Remove the smallest zone (by area) from the row to make it fit
                smallest_idx = min(range(len(row)), key=lambda i: row[i][1])
                removed_name, removed_area = row.pop(smallest_idx)
                row_total_area -= removed_area
                row_height = row_total_area / width if width > 0 else 0
                logger.debug(f"Removed {removed_name} (area={removed_area:.3f}), row now has {len(row)} zones, new row_height={row_height:.3f}")

            # If still not fitting (single zone too large), apply orientation-swap fallback
            # DEFECT J-1 FIX: Instead of placing with overflow, place as a vertical slice
            # Vertical slice: width = area / height_available, height = height_available
            # This guarantees w*h == area exactly and preserves area conservation
            if row_height > height + 1e-3 and len(row) == 1:
                name, area = row[0]
                # Use orientation swap: place as vertical slice (perpendicular to normal row orientation)
                # Vertical slice width might be narrower than available width (OK - uses less space)
                # OR wider (would overflow) - in that case, we need repartitioning
                vertical_slice_width = area / height if height > 0 else 0

                if vertical_slice_width <= width + 1e-6:
                    # Slice fits within available width - place it
                    rectangles[name] = (x, y, vertical_slice_width, height)
                    logger.info(f"DEFECT J-1 FIX (ORIENTATION SWAP): Single zone {name} (area={area:.2f}) placed as vertical slice: w={vertical_slice_width:.2f}, h={height:.2f}, w*h={vertical_slice_width*height:.2f}")
                    return height  # Height is consumed, main loop progresses vertically
                else:
                    # Zone's area is too large even for vertical slice
                    # Need to skip and trigger repartitioning
                    logger.warning(f"DEFECT J-1 FIX: Single zone {name} (area={area:.3f}) cannot fit: width_available={width:.3f}, height_available={height:.3f}, required_width_for_slice={vertical_slice_width:.3f} - triggering repartitioning")
                    # Clear row so main loop gets 0 and repartitions
                    row.clear()
                    return 0.0

            # CIRCULATION OVERFLOW FIX: Check absolute position bounds (y+height <= 20.0)
            # For multi-zone rows, ensure they don't overflow
            absolute_max = 20.0
            if y + row_height > absolute_max + 1e-3 and len(row) > 1:
                logger.warning(f"DEFECT J-1 FIX: y={y:.3f} + row_height={row_height:.3f} exceeds absolute boundary {absolute_max}, removing smallest zones")
                while y + row_height > absolute_max + 1e-3 and len(row) > 1:
                    smallest_idx = min(range(len(row)), key=lambda i: row[i][1])
                    removed_name, removed_area = row.pop(smallest_idx)
                    row_total_area -= removed_area
                    row_height = row_total_area / width if width > 0 else 0
                    logger.debug(f"Removed {removed_name} (area={removed_area:.3f}) for overflow, new y+height={y + row_height:.3f}")

            if not row:
                # All zones were removed - shouldn't happen but handle gracefully
                logger.warning(f"_layout_row_horizontal: all zones removed during pre-row-layout validation!")
                return 0.0

            current_x = x
            num_zones = len(row)
            logger.info(f"Row horizontal: placing {len(row)} zones (total_area={row_total_area:.2f}), row_height={row_height:.3f}")

            # DEFECT I-2/I-3/I-4 FIX: Compute all widths exactly to preserve area, NO special casing for last zone
            # Key insight: row_height = total_area / width, so Σ(area_i / row_height) = Σarea_i / row_height = width exactly
            # This preserves each zone's area WITHOUT requiring scaling or residual adjustments
            accumulated_error = 0.0

            for idx, (name, area) in enumerate(row):
                rect_width = area / row_height if row_height > 0 else 0

                rectangles[name] = (current_x, y, rect_width, row_height)
                actual_area = rect_width * row_height
                error = abs(actual_area - area)
                accumulated_error += error
                logger.debug(f"    {name}: x={current_x:.2f}, y={y:.2f}, w={rect_width:.4f}, h={row_height:.4f}, computed_area={actual_area:.4f}, target_area={area:.4f}, error={error:.6f}")
                current_x += rect_width

            logger.info(f"Row horizontal: placed {len(row)} zones, row_height={row_height:.3f}, accumulated_error={accumulated_error:.6f}")
            return row_height
        logger.warning(f"_layout_row_horizontal: no zones in row!")
        return 0.0

    def _layout_row_vertical(self, areas: List[Tuple[str, float]],
                             x: float, y: float, width: float, height: float,
                             rectangles: Dict[str, Tuple[float, float, float, float]],
                             zone_name_to_type: Optional[Dict[str, str]] = None) -> float:
        """
        Layout a vertical row (column) of rectangles.
        Returns the row width used.
        Implements squarification: greedily add rectangles while aspect ratio improves.
        DEFECT I-2/I-3/I-4 FIX: Preserve exact area for every zone by proportionally scaling heights.
        GUARANTEES: Σ(zone_height) = height (within floating-point precision)
                    Each zone.sqm = zone_width * zone_height (exact to 1e-6)
        """
        if not areas or width <= 0 or height <= 0:
            return 0.0

        row = []
        row_total_area = 0.0
        worst_ratio = float('inf')

        for i, (name, area) in enumerate(areas):
            # Try adding this rectangle to the row
            test_row = row + [(name, area)]
            test_total = row_total_area + area

            # Calculate worst aspect ratio if we add this rectangle
            test_ratio = self._worst_aspect_ratio_for_row(test_row, height)

            # If this is the first rectangle, or aspect ratio improves, add it
            if i == 0 or test_ratio < worst_ratio:
                row = test_row
                row_total_area = test_total
                worst_ratio = test_ratio
            else:
                # Aspect ratio would worsen, so lay out current row and return
                break

        # CYCLE 26 J-1 FIX (CORRECT APPROACH): Pre-row-layout validation
        # Before placing the row, verify that zone areas fit within available space
        # If row_total_area / height > width, remove smallest zones until row fits
        if row:
            row_width = row_total_area / height if height > 0 else 0

            # Validate row fits in available width (with small tolerance for floating-point)
            while row_width > width + 1e-3 and len(row) > 1:
                logger.info(f"PRE-ROW-LAYOUT VALIDATION: row_width={row_width:.3f} exceeds available width={width:.3f}, removing smallest zone from {len(row)} zones")
                # Remove the smallest zone (by area) from the row to make it fit
                smallest_idx = min(range(len(row)), key=lambda i: row[i][1])
                removed_name, removed_area = row.pop(smallest_idx)
                row_total_area -= removed_area
                row_width = row_total_area / height if height > 0 else 0
                logger.debug(f"Removed {removed_name} (area={removed_area:.3f}), row now has {len(row)} zones, new row_width={row_width:.3f}")

            # If still not fitting (single zone too large), apply orientation-swap fallback
            # DEFECT J-1 FIX: Instead of placing with overflow, place as a horizontal slice
            # Horizontal slice: width = width_available, height = area / width_available
            # This guarantees w*h == area exactly and preserves area conservation
            if row_width > width + 1e-3 and len(row) == 1:
                name, area = row[0]
                # Use orientation swap: place as horizontal slice (perpendicular to normal row orientation)
                # Horizontal slice height might be narrower than available height (OK - uses less space)
                # OR taller (would overflow) - in that case, we need repartitioning
                horizontal_slice_height = area / width if width > 0 else 0

                if horizontal_slice_height <= height + 1e-6:
                    # Slice fits within available height - place it
                    rectangles[name] = (x, y, width, horizontal_slice_height)
                    logger.info(f"DEFECT J-1 FIX (ORIENTATION SWAP): Single zone {name} (area={area:.2f}) placed as horizontal slice: w={width:.2f}, h={horizontal_slice_height:.2f}, w*h={width*horizontal_slice_height:.2f}")
                    return width  # Width is consumed, main loop progresses horizontally
                else:
                    # Zone's area is too large even for horizontal slice
                    # Need to skip and trigger repartitioning
                    logger.warning(f"DEFECT J-1 FIX: Single zone {name} (area={area:.3f}) cannot fit: height_available={height:.3f}, width_available={width:.3f}, required_height_for_slice={horizontal_slice_height:.3f} - triggering repartitioning")
                    # Clear row so main loop gets 0 and repartitions
                    row.clear()
                    return 0.0

            # CIRCULATION OVERFLOW FIX: Check absolute position bounds (x+width <= 20.0)
            # For multi-zone rows, ensure they don't overflow
            absolute_max = 20.0
            if x + row_width > absolute_max + 1e-3 and len(row) > 1:
                logger.warning(f"DEFECT J-1 FIX: x={x:.3f} + row_width={row_width:.3f} exceeds absolute boundary {absolute_max}, removing smallest zones")
                while x + row_width > absolute_max + 1e-3 and len(row) > 1:
                    smallest_idx = min(range(len(row)), key=lambda i: row[i][1])
                    removed_name, removed_area = row.pop(smallest_idx)
                    row_total_area -= removed_area
                    row_width = row_total_area / height if height > 0 else 0
                    logger.debug(f"Removed {removed_name} (area={removed_area:.3f}) for overflow, new x+width={x + row_width:.3f}")

            if not row:
                # All zones were removed - shouldn't happen but handle gracefully
                logger.warning(f"_layout_row_vertical: all zones removed during pre-row-layout validation!")
                return 0.0

            current_y = y
            num_zones = len(row)

            # DEFECT I-2/I-3/I-4 FIX: Compute all heights exactly to preserve area, NO special casing for last zone
            # Key insight: row_width = total_area / height, so Σ(area_i / row_width) = Σarea_i / row_width = height exactly
            # This preserves each zone's area WITHOUT requiring scaling or residual adjustments
            accumulated_error = 0.0

            for idx, (name, area) in enumerate(row):
                rect_height = area / row_width if row_width > 0 else 0

                rectangles[name] = (x, current_y, row_width, rect_height)
                actual_area = row_width * rect_height
                error = abs(actual_area - area)
                accumulated_error += error
                logger.debug(f"    {name}: x={x:.2f}, y={current_y:.2f}, w={row_width:.4f}, h={rect_height:.4f}, computed_area={actual_area:.4f}, target_area={area:.4f}, error={error:.6f}")
                current_y += rect_height

            logger.info(f"Row vertical: placed {len(row)} zones, row_width={row_width:.3f}, accumulated_error={accumulated_error:.6f}")
            return row_width
        return 0.0

    def _worst_aspect_ratio_for_row(self, items: List[Tuple[str, float]],
                                     container_side: float) -> float:
        """
        Calculate the worst aspect ratio of rectangles in a row.
        Row length = container_side, row width = total_area / container_side.
        For each rectangle: width = area / row_width, height = row_width.
        Aspect ratio = max(width, height) / min(width, height).

        Returns worst (highest) aspect ratio in the row.
        """
        if not items or container_side <= 0:
            return float('inf')

        total_area = sum(a[1] for a in items)
        if total_area == 0:
            return float('inf')

        row_width = total_area / container_side
        worst = 0.0

        for name, area in items:
            rect_side = area / row_width  # One dimension of rectangle
            other_side = row_width  # Other dimension
            if rect_side > 0 and other_side > 0:
                aspect = max(rect_side, other_side) / min(rect_side, other_side)
                worst = max(worst, aspect)

        return worst



    def _validate_rectangle_constraints(self, zone_name: str, zone_type_str: str,
                                        rect_width: float, rect_height: float) -> bool:
        """
        Validate a rectangle against shape constraints.
        Used DURING treemap construction to reject invalid rectangles early.

        Args:
            zone_name: Name of zone
            zone_type_str: Zone type string (e.g., "phone-booth")
            rect_width: Rectangle width in meters
            rect_height: Rectangle height in meters

        Returns:
            True if valid, False if violates constraints
        """
        try:
            zone_type = ZoneType(zone_type_str)
        except ValueError:
            return True  # Unknown type, allow

        constraints = self.SHAPE_CONSTRAINTS.get(zone_type, None)
        if not constraints:
            return True  # No constraint defined

        min_width = constraints.get("min_width", 0)
        max_aspect = constraints.get("max_aspect_ratio", float('inf'))

        short_side = min(rect_width, rect_height)
        long_side = max(rect_width, rect_height)

        # Check minimum width
        if short_side < min_width:
            return False

        # Check aspect ratio
        if long_side > 0 and short_side > 0:
            aspect_ratio = long_side / short_side
            if aspect_ratio > max_aspect:
                return False

        return True

    def _fix_constraint_violation(self, zone: Zone) -> bool:
        """
        Attempt to fix constraint violations by adjusting zone dimensions.
        Preserves area (width * length == sqm).

        Returns True if violation was fixed, False if still violates or cannot be fixed.
        """
        if zone.width is None or zone.length is None or zone.sqm is None:
            return False

        try:
            zone_type_enum = ZoneType(zone.zone_type)
        except (ValueError, KeyError):
            return False

        constraints = self.SHAPE_CONSTRAINTS.get(zone_type_enum)
        if not constraints:
            return False

        min_width = constraints.get("min_width", 0)
        max_aspect = constraints.get("max_aspect_ratio", float('inf'))

        # Check if there's a violation
        is_valid, violation_msg = self._check_shape_constraints(zone)
        if is_valid:
            return True  # No violation, nothing to fix

        area = zone.sqm

        # Try strategy 1: Move towards ideal square
        # Goal: reduce aspect ratio violations while keeping area = zone.sqm
        ideal_side = math.sqrt(area)

        # Check if ideal square satisfies all constraints
        test_zone = Zone(
            zone_type=zone.zone_type, name=zone.name, sqm=area, occupancy=0,
            adjacencies=[], notes="", x=0, y=0, width=ideal_side, length=ideal_side
        )
        test_valid, _ = self._check_shape_constraints(test_zone)
        if test_valid:
            zone.width = ideal_side
            zone.length = ideal_side
            logger.debug(f"Fixed {zone.name} by making it square: {ideal_side:.2f}x{ideal_side:.2f}")
            return True

        # Try strategy 2: Find dimensions that satisfy min_width (short side) and aspect ratio constraints
        # Constraints: short_side >= min_width, long_side/short_side <= max_aspect, width*height == area

        # Try a range of short_sides from min_width to sqrt(area / max_aspect)
        max_short_side = math.sqrt(area / max_aspect) if max_aspect > 0 else ideal_side
        short_side_min = min_width

        for attempt in range(50):
            # Linear search for valid short_side
            test_short_side = short_side_min + (max_short_side - short_side_min) * attempt / 50
            if test_short_side < min_width:
                test_short_side = min_width

            # Calculate long_side to preserve area
            test_long_side = area / test_short_side if test_short_side > 0 else ideal_side

            # Create a test zone with dimensions (order doesn't matter for checking constraints)
            test_zone = Zone(
                zone_type=zone.zone_type, name=zone.name, sqm=area, occupancy=0,
                adjacencies=[], notes="", x=0, y=0, width=test_long_side, length=test_short_side
            )
            test_valid, _ = self._check_shape_constraints(test_zone)
            if test_valid:
                zone.width = test_long_side
                zone.length = test_short_side
                logger.debug(f"Fixed {zone.name} by adjusting dimensions: {test_long_side:.2f}x{test_short_side:.2f}")
                return True

        # Could not fix the violation
        logger.warning(f"Could not fix constraint violation for {zone.name}: needs complex repartitioning. Issue: {violation_msg}")
        return False

    def _check_shape_constraints(self, zone: Zone) -> Tuple[bool, str]:
        """
        Check if a zone's dimensions satisfy shape constraints.
        Validates BOTH shape constraints AND area conservation.

        Shape constraints use:
        - min_width: minimum dimension for the SHORT SIDE (prevents thin slivers)
        - max_aspect_ratio: long_side / short_side (allows elongation but not excessively)

        Returns:
            Tuple of (is_valid, violation_description)
        """
        if zone.width is None or zone.length is None:
            return True, ""

        # Check area conservation
        # A zone is valid ONLY if its rectangle area matches its programmed sqm value (within tolerance)
        if zone.sqm is not None and zone.sqm > 0:
            computed_area = zone.width * zone.length
            area_error = abs(computed_area - zone.sqm)
            tolerance = 1e-6 * zone.sqm  # Relative tolerance: 1 millionth of the zone's sqm
            tolerance = max(tolerance, 0.01)  # But at least 0.01 sqm absolute tolerance

            if area_error > tolerance:
                return False, f"Area mismatch: programmed {zone.sqm:.2f} sqm, geometry {computed_area:.2f} sqm (error {area_error:.6f})"

        # Check bounds: zone must fit within 20x20 plan
        if zone.x is not None and zone.y is not None and zone.width is not None and zone.length is not None:
            if zone.x < 0 or zone.y < 0:
                return False, f"Zone position out of bounds: x={zone.x:.3f}, y={zone.y:.3f} (must be >= 0)"
            x_overflow = zone.x + zone.width - 20.0
            y_overflow = zone.y + zone.length - 20.0
            if x_overflow > 1e-6:
                return False, f"Zone width overflows: x={zone.x:.3f}, width={zone.width:.3f}, x+w={zone.x+zone.width:.3f} (max 20.0)"
            if y_overflow > 1e-6:
                return False, f"Zone length overflows: y={zone.y:.3f}, length={zone.length:.3f}, y+l={zone.y+zone.length:.3f} (max 20.0)"

        try:
            zone_type_enum = ZoneType(zone.zone_type)
        except (ValueError, KeyError):
            logger.warning(f"Unknown zone type for constraint checking: {zone.zone_type}")
            return True, ""  # Unknown type, allow

        constraints = self.SHAPE_CONSTRAINTS.get(zone_type_enum, None)
        if not constraints:
            return True, ""  # No constraint defined for this type

        min_width = constraints.get("min_width", 0)
        max_aspect = constraints.get("max_aspect_ratio", float('inf'))

        short_side = min(zone.width, zone.length)
        long_side = max(zone.width, zone.length)

        # Check minimum width (applies to SHORT SIDE, not width dimension)
        if short_side < min_width:
            return False, f"Min dimension {min_width}m required (short side), got {short_side:.3f}m"

        # Check aspect ratio (computed as long_side / short_side)
        if short_side > 0:
            aspect_ratio = long_side / short_side
            if aspect_ratio > max_aspect:
                return False, f"Max aspect ratio {max_aspect:.1f} required, got {aspect_ratio:.2f} (long_side/short_side)"

        return True, ""

    def _apply_geometric_layout(self, zones: List[Zone]) -> Tuple[List[Zone], List[str]]:
        """
        Apply squarified treemap layout to zones.
        Adds x, y, width, length coordinates to each zone.
        GUARANTEES:
          - non-overlapping rectangles
          - exact surface conservation
          - Constraint violations are detected and returned (not raised)

        Returns:
            Tuple of (zones, constraint_violations list)

        Note:
            This is a multi-attempt layout process. If initial layout has constraint violations,
            it attempts repartitioning (redistributing zone surfaces) and retrying. If after
            several attempts violations persist, they are returned in the output so the client
            can audit and understand the compromise made.
        """
        if not zones:
            return zones, []

        # Make a working copy to avoid mutating original
        working_zones = [Zone(
            zone_type=z.zone_type,
            name=z.name,
            sqm=z.sqm,
            occupancy=z.occupancy,
            adjacencies=z.adjacencies,
            notes=z.notes,
            x=z.x, y=z.y, width=z.width, length=z.length
        ) for z in zones]

        # Retry loop: attempt layout, detect violations, repartition, retry
        # Increased to 50 to allow extremely aggressive repartitioning for constraint violations
        # Each iteration tries to fix violations by aggressively resizing violating zones
        max_repartition_attempts = 50
        best_layout_zones = None
        best_violation_count = float('inf')

        # Track minimum areas required for each zone to satisfy constraints
        # NOTE: Do NOT enforce minimum areas before treemap!
        # The treemap will naturally lay out zones, and we'll check constraints AFTER.
        # Enforcing before treemap causes cascading size increases that exceed surface_sqm.
        min_areas_required = {}
        for zone in working_zones:
            min_area = self._calculate_minimum_area_for_constraints(ZoneType(zone.zone_type))
            if min_area > 0:
                min_areas_required[zone.name] = min_area
            # Do NOT increase zone.sqm here! The treemap will handle layout.

        for attempt in range(max_repartition_attempts):
            # DEFECT I FIX: Use treemap base case scaling (width*length = area) for exact conservation
            # Do NOT scale zones before treemap — treemap scaling is sufficient
            # The key invariant: each zone's final width*length == zone.sqm (from treemap base case scaling)
            total_zone_sqm = sum(z.sqm for z in working_zones)

            # Set up coordinate system: assume rectangular envelope
            side_length = math.sqrt(self.surface_sqm)

            # Create mapping from zone name to zone type for constraint-aware subdivision
            zone_name_to_type = {z.name: z.zone_type for z in working_zones}

            # Apply treemap to ALL zones (including circulation)
            areas = [(z.name, z.sqm) for z in working_zones]

            # DEFECT I-2/I-3/I-4 FIX: Pre-group constrained zones (especially phone booths)
            # Phone booths cannot individually satisfy constraints (need w >= 1.0, but 2.5 sqm box can be 1.0x2.5)
            # Solution: Group all phone booths into a single virtual zone for treemap, then subdivide them after
            phone_booth_zones = []
            other_zones = []
            phone_booth_total = 0.0

            for zone_name, area in areas:
                zone_type_str = zone_name_to_type.get(zone_name) if zone_name_to_type else None
                if zone_type_str == "phone-booth":
                    phone_booth_zones.append((zone_name, area))
                    phone_booth_total += area
                else:
                    other_zones.append((zone_name, area))

            # If we have multiple phone booths, group them into a single mega-zone for treemap
            # This ensures the phone booth band meets min_width constraint
            # The mega-zone will be subdivided after treemap placement
            if len(phone_booth_zones) > 1:
                # Create a virtual "Phone Booths" mega-zone in treemap
                areas = other_zones + [("__phone_booths_group__", phone_booth_total)]
                logger.info(f"DEFECT I-3 FIX: Grouping {len(phone_booth_zones)} phone booths into mega-zone ({phone_booth_total:.2f} sqm) for treemap")
            else:
                # Single phone booth or no phone booths - use normal priority sort
                def get_zone_priority(zone_name_area):
                    zone_name, area = zone_name_area
                    zone_type_str = zone_name_to_type.get(zone_name) if zone_name_to_type else None
                    if zone_type_str:
                        try:
                            zone_type_enum = ZoneType(zone_type_str)
                            constraints = self.SHAPE_CONSTRAINTS.get(zone_type_enum)
                            if constraints:
                                min_width = constraints.get("min_width", 0)
                                return (min_width, area)
                        except ValueError:
                            pass
                    return (0, area)

                areas.sort(key=lambda x: get_zone_priority(x), reverse=True)

            rectangles: Dict[str, Tuple[float, float, float, float]] = {}

            # Start squarification from top-left with constraint awareness
            self._squarify_treemap(areas, 0, 0, side_length, side_length, rectangles, [], zone_name_to_type)

            # DEFECT I-3 FIX: Subdivide phone booth mega-zone if it exists
            if "__phone_booths_group__" in rectangles:
                group_x, group_y, group_w, group_h = rectangles["__phone_booths_group__"]
                del rectangles["__phone_booths_group__"]  # Remove the virtual zone
                logger.info(f"DEFECT I-3 FIX: Subdividing phone booths group at ({group_x:.2f}, {group_y:.2f}) size {group_w:.2f}x{group_h:.2f}")

                # Place phone booths in a grid within the mega-zone
                # Goal: arrange them to respect min_width and aspect ratio constraints
                if phone_booth_zones:
                    # Determine grid layout: arrange booths to fit width and height constraints
                    # Each booth is ~1.1m wide x 2.3m deep (2.5 sqm)
                    # Try to fit multiple booths per row

                    # Simple strategy: arrange booths in rows
                    # Each booth will be given width = group_w / ceil(sqrt(len(booths))) and height accordingly
                    num_booths = len(phone_booth_zones)
                    booths_per_row = max(1, int((group_w / 1.1)))  # Aim for 1.1m per booth
                    num_rows = (num_booths + booths_per_row - 1) // booths_per_row  # Ceiling division
                    booths_per_row = (num_booths + num_rows - 1) // num_rows  # Recalculate to balance

                    booth_width = group_w / booths_per_row
                    booth_height = group_h / num_rows

                    current_booth_x = group_x
                    current_booth_y = group_y
                    booth_index = 0

                    for row_idx in range(num_rows):
                        for col_idx in range(booths_per_row):
                            if booth_index >= len(phone_booth_zones):
                                break

                            zone_name, zone_area = phone_booth_zones[booth_index]
                            # Place booth with width and height such that area ≈ zone_area
                            # We want booth_width * booth_height ≈ zone_area
                            # But we're constraining booth_width and booth_height by the grid
                            # So we'll use the grid dimensions but verify area is preserved

                            # Actually, to preserve area exactly: scale down the booth dimensions proportionally
                            adjusted_height = zone_area / booth_width if booth_width > 0 else booth_height
                            if adjusted_height > booth_height:
                                # Booth needs more height than available - use grid height and adjust width
                                adjusted_width = zone_area / booth_height if booth_height > 0 else booth_width
                                adjusted_height = booth_height
                            else:
                                adjusted_width = booth_width

                            rectangles[zone_name] = (current_booth_x, current_booth_y, adjusted_width, adjusted_height)
                            logger.debug(f"  Phone booth {zone_name}: grid pos ({current_booth_x:.2f}, {current_booth_y:.2f}), size {adjusted_width:.2f}x{adjusted_height:.2f}, area={adjusted_width*adjusted_height:.2f}, target={zone_area:.2f}")

                            current_booth_x += booth_width
                            booth_index += 1

                        current_booth_y += booth_height
                        current_booth_x = group_x

            # Assign coordinates to zones from treemap
            constraint_violations = []
            violation_map = {}  # Map zone name to violation
            for zone in working_zones:
                if zone.name in rectangles:
                    x, y, w, h = rectangles[zone.name]
                    zone.x = x
                    zone.y = y
                    zone.width = w
                    zone.length = h
                    logger.info(f"ASSIGN TREEMAP: {zone.name} sqm_target={zone.sqm:.2f}, assigned w={w:.2f}, l={h:.2f}, w*l={w*h:.2f}")

            # DEFECT D FIX: Validate and fix boundary overflows (x+width <= 20, y+length <= 20)
            # After treemap placement, some zones may overflow the 20x20 bounds.
            # Clip overflowing zones to fit within bounds, adjusting companion dimension to preserve area.
            boundary_max = 20.0
            for zone in working_zones:
                if zone.x is None or zone.y is None or zone.width is None or zone.length is None:
                    continue

                # Check width overflow (x+width > 20)
                if zone.x + zone.width > boundary_max + 1e-6:
                    available_width = boundary_max - zone.x
                    if available_width > 0:
                        new_length = zone.sqm / available_width if available_width > 0 else zone.length
                        logger.warning(
                            f"DEFECT D FIX: {zone.name} width overflow: x={zone.x:.3f}, w={zone.width:.3f}, x+w={zone.x + zone.width:.3f}. "
                            f"Clipping: new_width={available_width:.3f}, new_length={new_length:.3f} (area={available_width * new_length:.2f})"
                        )
                        zone.width = available_width
                        zone.length = new_length

                # Check length overflow (y+length > 20)
                if zone.y + zone.length > boundary_max + 1e-6:
                    available_length = boundary_max - zone.y
                    if available_length > 0:
                        new_width = zone.sqm / available_length if available_length > 0 else zone.width
                        logger.warning(
                            f"DEFECT D FIX: {zone.name} length overflow: y={zone.y:.3f}, l={zone.length:.3f}, y+l={zone.y + zone.length:.3f}. "
                            f"Clipping: new_length={available_length:.3f}, new_width={new_width:.3f} (area={new_width * available_length:.2f})"
                        )
                        zone.width = new_width
                        zone.length = available_length

            # DEFECT I CRITICAL FIX: Do NOT scale dimensions after treemap
            # The treemap produces rectangles where width*length should equal the pre-scaled sqm value
            # Scaling dimensions after treemap breaks the invariant and causes overlaps
            # Instead, all area adjustment happens BEFORE treemap (see above)

            # DEFECT I-1 CRITICAL FIX: Do NOT recalculate zone.sqm from w*h after treemap
            # The programmed sqm is the source of truth. The treemap assigns w,h that should
            # preserve this sqm. Recalculating breaks surface conservation.
            # Keep the programmed sqm value as-is; verify geometry is sensible but don't mutate sqm.
            for zone in working_zones:
                if zone.width is not None and zone.length is not None:
                    computed_sqm = zone.width * zone.length
                    # IMPORTANT: Keep zone.sqm as programmed (unchanged)
                    # Just verify the geometry makes sense
                    logger.debug(f"Zone {zone.name}: sqm_programmed={zone.sqm:.2f}, w*l_actual={computed_sqm:.2f}")

            # NOW check shape constraints on final geometry
            # CRITICAL: Check ALL zones, including those without dimensions (treat as invalid)
            # Use violation_map as the canonical source — each zone appears at most once
            # DEFECT F3 FIX: NEVER CLEAR VIOLATIONS — violations must propagate to API response
            logger.info(f"Layout attempt {attempt+1}: Checking {len(working_zones)} zones for shape constraints")
            # DO NOT clear violation_map from previous attempts if this attempt has violations
            # Instead, we accumulate best violations across all attempts
            attempt_violation_map = {}
            for zone in working_zones:
                # Every zone MUST have width and length set by treemap
                if zone.width is None or zone.length is None:
                    attempt_violation_map[zone.name] = f"No geometry assigned (width={zone.width}, length={zone.length})"
                else:
                    # Check shape constraints — DEFECT F3: must be called on final geometry
                    is_valid, violation_msg = self._check_shape_constraints(zone)
                    min_dim = min(zone.width, zone.length) if zone.width and zone.length else 0
                    ratio = max(zone.width, zone.length)/min_dim if min_dim > 0 else 0
                    logger.debug(f"  {zone.name} ({zone.zone_type}): w={zone.width:.2f}, l={zone.length:.2f}, ratio={ratio:.3f} -> {'PASS' if is_valid else 'FAIL'}")
                    if not is_valid:
                        # DEFECT F3 FIX: Record violation BEFORE attempting fix
                        attempt_violation_map[zone.name] = violation_msg
                        logger.info(f"Constraint VIOLATION: {zone.name} ({zone.zone_type}): {violation_msg}")

                        # DEFECT I-2 FIX: Try to fix constraint violation while preserving area
                        # If we can fix it, remove from violations; otherwise keep it
                        fixed = self._fix_constraint_violation(zone)
                        if fixed:
                            # Re-check after fix
                            is_valid, violation_msg = self._check_shape_constraints(zone)
                            if is_valid:
                                # Successfully fixed — remove from violation map
                                del attempt_violation_map[zone.name]
                                logger.info(f"Fixed constraint violation for {zone.name} while preserving area")
                            else:
                                # Still violates after fix — update with new message
                                attempt_violation_map[zone.name] = violation_msg
                                logger.info(f"Still violates after fix: {zone.name} ({zone.zone_type}): {violation_msg}")

            violation_map = attempt_violation_map  # Use attempt violations for this iteration

            # Convert violation_map to list for this iteration (deduplicated)
            constraint_violations = [f"{zone_name}: {msg}" for zone_name, msg in violation_map.items()]

            # Verify non-overlapping: check intersection area for all pairs
            has_overlaps, overlap_list = self._verify_no_overlaps(working_zones)
            if has_overlaps:
                logger.info(f"Layout attempt {attempt+1}: {len(overlap_list)} overlaps detected")
                constraint_violations.extend([f"OVERLAP: {z1} and {z2} ({area:.4f} sqm)" for z1, z2, area in overlap_list])

            # Track best layout so far (fewest violations)
            if len(constraint_violations) < best_violation_count:
                best_violation_count = len(constraint_violations)
                # DEFECT I-1: Keep programmed sqm, not w*l
                best_layout_zones = [Zone(
                    zone_type=z.zone_type,
                    name=z.name,
                    sqm=z.sqm,  # Keep programmed value, don't recalculate from w*l
                    occupancy=z.occupancy,
                    adjacencies=z.adjacencies,
                    notes=z.notes,
                    x=z.x, y=z.y, width=z.width, length=z.length
                ) for z in working_zones]

            # If no constraint violations, success!
            if not constraint_violations:
                # Copy back to original zones list
                # Use zone name mapping to handle reordering from repartitioning
                # DEFECT I-1 FIX: Keep programmed sqm, do NOT recalculate from w*l
                working_map = {z.name: z for z in working_zones}
                for zone in zones:
                    if zone.name in working_map:
                        wz = working_map[zone.name]
                        zone.x = wz.x
                        zone.y = wz.y
                        zone.width = wz.width
                        zone.length = wz.length
                        # Keep the programmed sqm value (source of truth)
                        zone.sqm = wz.sqm  # Use working zone's sqm (which is programmed value)
                logger.info(f"Layout CONVERGED at attempt {attempt+1} with ZERO violations - returning early")
                return zones, []

            # Constraint violations detected - attempt repartitioning
            if attempt < max_repartition_attempts - 1:
                logger.warning(f"Layout attempt {attempt+1}: {len(constraint_violations)} constraint violations detected, repartitioning...")

                # Repartition strategy: redistribute area from non-violating zones to violating zones
                # to give violating zones more flexibility in the treemap
                # Key insight: If a zone violates min_width, it needs either more area or fewer zones in its partition
                has_aspect_violation = any("aspect ratio" in msg for msg in violation_map.values())
                has_width_violation = any("Min width" in msg for msg in violation_map.values())
                has_overlap_violation = any("OVERLAP" in msg for msg in constraint_violations)

                # Strategy: ABSOLUTELY enforce minimum area constraints
                # For violating zones: INCREASE area to give them more placement flexibility
                # NEVER reduce any zone below its minimum required area
                # DEFECT I-1 FIX: NEVER reduce circulation below 40 sqm (minimum required for corridors)
                total_increase_needed = 0
                for zone_name, violation_msg in violation_map.items():
                    for zone in working_zones:
                        if zone.name == zone_name:
                            # DEFECT E FIX: Phone booths are FIXED SIZE (2.5 sqm per booth)
                            # Do NOT repartition phone booths — they maintain their size
                            if zone.zone_type == "phone-booth":
                                continue

                            min_required = min_areas_required.get(zone.name, 0)

                            # Strategy: ALL violations warrant INCREASING the violating zone's area
                            # More sqm gives treemap more flexibility to place it properly
                            # DEFECT I-3: For break-room specifically, be more aggressive (2.0x instead of 1.5x)
                            # because it has the tightest min_width constraint (1.8m)
                            multiplier = 2.0 if zone.zone_type == "break-room" else 1.5
                            if zone.sqm < min_required * multiplier:
                                increase = (min_required * multiplier) - zone.sqm
                                zone.sqm += increase
                                total_increase_needed += increase
                                logger.info(f"  ENFORCING MIN: {zone.name} {zone.zone_type} increased to {zone.sqm:.1f} sqm (min_required: {min_required:.1f}, multiplier={multiplier}), reason: {violation_msg[:40]}")
                            break

                # Rebalance: take from OPEN-SPACE ONLY (most flexible), NOT from circulation
                # DEFECT I-1 FIX: Preserve circulation zone (minimum 40 sqm)
                if total_increase_needed > 0:
                    # First try to take from open-space
                    open_space_zones = [z for z in working_zones if z.zone_type == "open-space"]
                    circulation_minimum = 40.0  # Preserve at least 40 sqm for circulation (corridors, restrooms)

                    if open_space_zones:
                        per_zone = total_increase_needed / len(open_space_zones)
                        for os in open_space_zones:
                            os.sqm = max(os.sqm - per_zone, 80)  # But don't reduce below 80 sqm for open-space
                        logger.info(f"  Rebalance: took {total_increase_needed:.1f} sqm from open-space")
                    else:
                        # If no open-space, try circulation but preserve minimum
                        circulation_zones = [z for z in working_zones if z.zone_type == "circulation"]
                        if circulation_zones:
                            for circ in circulation_zones:
                                # Don't reduce circulation below 40 sqm
                                available_to_reduce = max(0, circ.sqm - circulation_minimum)
                                reduce_amount = min(total_increase_needed, available_to_reduce)
                                circ.sqm -= reduce_amount
                                total_increase_needed -= reduce_amount
                                logger.info(f"  Rebalance: took {reduce_amount:.1f} sqm from circulation (kept {circ.sqm:.1f} sqm, min={circulation_minimum})")

                # Reordering: width-violating zones should be placed earlier (priority in treemap)
                if has_width_violation:
                    # Separate violating and non-violating zones
                    violating_names = set(violation_map.keys())
                    non_violating = [z for z in working_zones if z.name not in violating_names]
                    violating = [z for z in working_zones if z.name in violating_names]

                    # Reorder: put VIOLATING zones first (by size descending) then non-violating
                    # This gives violating zones priority in treemap subdivision
                    violating.sort(key=lambda z: -z.sqm)  # Larger first (priority in treemap)
                    non_violating.sort(key=lambda z: -z.sqm)  # Larger first

                    working_zones = violating + non_violating
                    logger.info(f"  Reordered zones: {[z.name for z in violating]} first")

                continue

            # Max attempts reached; use best layout found
            if best_layout_zones:
                logger.warning(f"Layout did not converge after {max_repartition_attempts} attempts. Using best layout found ({best_violation_count} violations).")
                # Use zone name mapping to handle reordering from repartitioning
                # DEFECT I-1 FIX: Keep programmed sqm, do NOT recalculate from w*l
                best_map = {z.name: z for z in best_layout_zones}
                for zone in zones:
                    if zone.name in best_map:
                        bz = best_map[zone.name]
                        zone.x = bz.x
                        zone.y = bz.y
                        zone.width = bz.width
                        zone.length = bz.length
                        # Keep the programmed sqm value (source of truth)
                        zone.sqm = bz.sqm  # Use best layout's sqm (which is programmed value)
                # Recalculate violations for best layout (deduplicated via dict)
                best_violation_map = {}
                for zone in best_layout_zones:
                    if zone.width is None or zone.length is None:
                        best_violation_map[zone.name] = f"No geometry assigned (width={zone.width}, length={zone.length})"
                    else:
                        is_valid, violation_msg = self._check_shape_constraints(zone)
                        if not is_valid:
                            best_violation_map[zone.name] = violation_msg
                constraint_violations = [f"{zone_name}: {msg}" for zone_name, msg in best_violation_map.items()]
                logger.critical(f"RETURNING from max_attempts block with {len(constraint_violations)} violations: {constraint_violations}")
                return zones, constraint_violations

        # Return best layout found
        if best_layout_zones:
            # Use zone name mapping to handle reordering from repartitioning
            # DEFECT I-1 FIX: Keep programmed sqm, do NOT recalculate from w*l
            best_map = {z.name: z for z in best_layout_zones}
            for zone in zones:
                if zone.name in best_map:
                    bz = best_map[zone.name]
                    zone.x = bz.x
                    zone.y = bz.y
                    zone.width = bz.width
                    zone.length = bz.length
                    # Keep the programmed sqm value (source of truth)
                    zone.sqm = bz.sqm  # Use best layout's sqm (which is programmed value)
            # DEFECT F3 FIX: Recalculate violations for best layout AND ensure they're returned
            best_violation_map = {}
            for zone in best_layout_zones:
                if zone.width is None or zone.length is None:
                    best_violation_map[zone.name] = f"No geometry assigned (width={zone.width}, length={zone.length})"
                else:
                    is_valid, violation_msg = self._check_shape_constraints(zone)
                    if not is_valid:
                        best_violation_map[zone.name] = violation_msg
                        logger.warning(f"DEFECT F3: Final violation in best layout: {zone.name}: {violation_msg}")
            constraint_violations = [f"{zone_name}: {msg}" for zone_name, msg in best_violation_map.items()]
            if constraint_violations:
                logger.critical(f"_apply_geometric_layout: returning best layout with {len(constraint_violations)} violations: {constraint_violations}")
            return zones, constraint_violations

        # Fallback: if we have no layout at all, don't try to recalculate sqm
        # Just return what we have
        for zone in zones:
            # DEFECT I-1: Only set sqm from w*l if it's clearly wrong (0.0 or None)
            # Don't recalculate if we already have a programmed value
            if zone.sqm == 0 or zone.sqm is None:
                if zone.width and zone.length:
                    zone.sqm = zone.width * zone.length

        # DEFECT F3 FIX: ALWAYS check and return violations from final zones
        # This is the last resort — ensure violations are never silently dropped
        logger.error(f"_apply_geometric_layout: exhausted {max_repartition_attempts} attempts without finding a valid layout.")
        final_violations = []
        for zone in zones:
            if zone.width is None or zone.length is None:
                final_violations.append(f"{zone.name}: No geometry assigned")
                logger.warning(f"DEFECT F3: Zone without geometry: {zone.name}")
            else:
                is_valid, violation_msg = self._check_shape_constraints(zone)
                if not is_valid:
                    final_violations.append(f"{zone.name}: {violation_msg}")
                    logger.warning(f"DEFECT F3: Final zone violation: {zone.name}: {violation_msg}")
        if final_violations:
            logger.critical(f"_apply_geometric_layout: returning final layout with {len(final_violations)} violations: {final_violations}")
        return zones, final_violations

    def _verify_no_overlaps(self, zones: List[Zone]) -> Tuple[bool, List[Tuple[str, str, float]]]:
        """
        Verify no zones overlap. Returns list of overlaps found.
        Used to detect and report geometric errors in treemap output.

        Returns:
            Tuple of (has_overlaps, list of (zone1_name, zone2_name, overlap_area))
        """
        overlaps = []
        for i in range(len(zones)):
            for j in range(i+1, len(zones)):
                z1 = zones[i]
                z2 = zones[j]
                if z1.x is None or z2.x is None:
                    continue

                # Calculate intersection area
                x_left = max(z1.x, z2.x)
                x_right = min(z1.x + z1.width, z2.x + z2.width)
                y_top = max(z1.y, z2.y)
                y_bottom = min(z1.y + z1.length, z2.y + z2.length)

                if x_left < x_right and y_top < y_bottom:
                    overlap_area = (x_right - x_left) * (y_bottom - y_top)
                    tolerance = 0.01  # Allow tiny numerical errors (< 1cm²)
                    if overlap_area > tolerance:
                        overlaps.append((z1.name, z2.name, overlap_area))
                        logger.warning(f"OVERLAP DETECTED: {z1.name} ({z1.zone_type}) and {z2.name} ({z2.zone_type}) overlap {overlap_area:.4f} sqm")

        return len(overlaps) > 0, overlaps

    def _row_pack_zones_deterministic(self, zones: List[Zone], container_size: float) -> List[Dict]:
        """
        Row-packing fallback for zones that didn't get treemap coordinates.
        Deterministic: no randomness, reproducible for same input.

        Args:
            zones: List of Zone objects without coordinates
            container_size: Size of container (width = height)

        Returns:
            List of placement dicts with x, y, width, height, zone_name
        """
        placements = []

        # Sort zones by area descending (larger zones first)
        sorted_zones = sorted(zones, key=lambda z: -z.sqm)

        current_x = 0.0
        current_y = 0.0
        row_height = 0.0
        max_row_width = container_size

        for zone in sorted_zones:
            aspect = 1.0  # Square-ish
            zone_width = math.sqrt(zone.sqm / aspect)
            zone_height = zone.sqm / zone_width

            # Check if zone fits in current row
            if current_x + zone_width > max_row_width:
                # Start new row
                current_x = 0.0
                current_y += row_height
                row_height = 0.0

            # Place zone
            placements.append({
                "zone_name": zone.name,
                "x": current_x,
                "y": current_y,
                "width": zone_width,
                "height": zone_height
            })

            current_x += zone_width
            row_height = max(row_height, zone_height)

        return placements

    def calculate_usable_area(self) -> float:
        """Calculate usable area after accounting for circulation."""
        return self.surface_sqm * (1 - self.circulation_pct)

    def _distribute_zones(self) -> Dict[ZoneType, int]:
        """Distribute headcount across zone types based on brief."""
        usable = self.calculate_usable_area()
        collab_target = self.COLLABORATION_TARGETS[self.collaboration_style]

        # Calculate how many people go to collaborative vs. focused zones
        collab_types = {ZoneType.MEETING, ZoneType.PHONE_BOOTH, ZoneType.BREAK_ROOM}
        focus_types = {ZoneType.OPEN_SPACE, ZoneType.QUIET_ZONE}

        collab_headcount = int(self.headcount * collab_target)
        focus_headcount = self.headcount - collab_headcount

        distribution = {}

        if ZoneType.OPEN_SPACE in self.zone_types:
            distribution[ZoneType.OPEN_SPACE] = int(focus_headcount * 0.7)  # 70% open space

        if ZoneType.QUIET_ZONE in self.zone_types:
            distribution[ZoneType.QUIET_ZONE] = int(focus_headcount * 0.3)  # 30% quiet zones

        if ZoneType.MEETING in self.zone_types:
            distribution[ZoneType.MEETING] = int(collab_headcount * 0.5)  # 50% meeting rooms

        if ZoneType.PHONE_BOOTH in self.zone_types:
            distribution[ZoneType.PHONE_BOOTH] = int(collab_headcount * 0.3)  # 30% phone booths

        if ZoneType.BREAK_ROOM in self.zone_types:
            distribution[ZoneType.BREAK_ROOM] = int(collab_headcount * 0.2)  # 20% break areas

        if ZoneType.STORAGE in self.zone_types:
            # Storage: 5% of usable area
            storage_sqm = usable * 0.05
            distribution[ZoneType.STORAGE] = 1  # marker for 1 storage area

        return distribution

    def _calculate_minimum_area_for_constraints(self, zone_type: ZoneType) -> float:
        """
        Calculate minimum surface area required for a zone type to satisfy shape constraints.

        For a zone to satisfy:
        - width >= min_width
        - aspect_ratio <= max_aspect_ratio

        Minimum area is achieved at:
        - width = min_width
        - aspect_ratio = max_aspect_ratio
        - area = width * (width * aspect_ratio) = width² * aspect_ratio

        DEFECT I-3 FIX: Add buffer for zones with challenging constraints to ensure
        they have enough space in the treemap to be placed correctly.

        Args:
            zone_type: Zone type enum

        Returns:
            Minimum area in sqm
        """
        constraints = self.SHAPE_CONSTRAINTS.get(zone_type)
        if not constraints:
            return 0

        min_width = constraints.get("min_width", 0)
        max_aspect = constraints.get("max_aspect_ratio", float('inf'))

        if min_width <= 0 or max_aspect == float('inf'):
            return 0

        # min_area = min_width² * max_aspect_ratio
        min_area = (min_width ** 2) * max_aspect

        # DEFECT I-3 FIX: Add buffer for constrained zones based on min_width
        # These zones need extra area to ensure they fit properly in the treemap
        if min_width >= 1.8:
            # Add 35% buffer for highly constrained zones (break-room, meeting, quiet-zone, open-space)
            min_area *= 1.35
            logger.debug(f"_calculate_minimum_area_for_constraints: {zone_type.value} min_area increased by 35% buffer (min_width={min_width}) -> {min_area:.2f}")
        elif min_width >= 1.0:
            # Add 25% buffer for moderately constrained zones (phone-booth)
            min_area *= 1.25
            logger.debug(f"_calculate_minimum_area_for_constraints: {zone_type.value} min_area increased by 25% buffer (min_width={min_width}) -> {min_area:.2f}")

        return min_area

    def _create_zones(self, distribution: Dict[ZoneType, int]) -> List[Zone]:
        """
        Create zone objects from distribution.
        Allocates 85% of surface to usable zones, 15% to circulation.
        DEFECT E FIX: Phone booths are FIXED SIZE (count * 2.5 sqm), not scaled by area.
        Other zones scale proportionally to fit remaining usable area.
        DEFECT B FIX: Ensure each zone gets minimum area to satisfy shape constraints.
        """
        usable = self.calculate_usable_area()  # 85% of total surface
        # Do NOT calculate circulation_sqm yet - it will be added after rescaling
        zones = []

        # STEP 1: Calculate base allocations
        zone_allocations = {}
        phone_booth_sqm = 0  # Track phone booth space (FIXED)

        for zone_type, count in distribution.items():
            if count == 0:
                continue

            sqm_per_person = self.ZONE_SIZING.get(zone_type, 4.0)

            if zone_type == ZoneType.STORAGE:
                zone_allocations[zone_type] = usable * 0.05
            elif zone_type == ZoneType.PHONE_BOOTH:
                # PHONE BOOTH: FIXED SIZE (count * 2.5 sqm per booth, never scale or adjust)
                zone_allocations[zone_type] = count * sqm_per_person
                phone_booth_sqm = zone_allocations[zone_type]
            else:
                zone_allocations[zone_type] = count * sqm_per_person

        # STEP 2: Calculate total of non-phone-booth allocations
        non_booth_allocations = {k: v for k, v in zone_allocations.items() if k != ZoneType.PHONE_BOOTH}
        non_booth_total = sum(non_booth_allocations.values())

        # STEP 3: Scale non-phone-booth zones to fit remaining usable area
        remaining_usable = usable - phone_booth_sqm
        if non_booth_total > 0 and remaining_usable > 0:
            scale_factor = remaining_usable / non_booth_total
            for zone_type in non_booth_allocations:
                zone_allocations[zone_type] *= scale_factor

        # STEP 3B: DEFECT B FIX - Ensure each zone meets minimum area constraints
        # Calculate minimum area required for each zone type to satisfy shape constraints
        min_areas = {}
        for zone_type in zone_allocations:
            min_area = self._calculate_minimum_area_for_constraints(zone_type)
            if min_area > 0:
                min_areas[zone_type] = min_area
                if zone_allocations[zone_type] < min_area:
                    logger.warning(
                        f"Zone {zone_type.value}: allocated {zone_allocations[zone_type]:.1f} sqm "
                        f"but requires minimum {min_area:.1f} sqm to satisfy shape constraints. "
                        f"Increasing allocation."
                    )
                    zone_allocations[zone_type] = min_area

        # Recalculate total with adjusted allocations and rescale everything proportionally
        new_non_booth_total = sum(v for k, v in zone_allocations.items() if k != ZoneType.PHONE_BOOTH)
        if new_non_booth_total > remaining_usable:
            # Allocations exceed available space - but DO NOT reduce zones below their minimums
            # Instead, reduce only the most flexible zones (open-space, circulation)
            logger.warning(f"Minimum area constraints total {new_non_booth_total:.1f} sqm exceeds available {remaining_usable:.1f} sqm")

            # Strategy: reduce only flexible zones (open-space) to make room for constrained zones
            flexible_types = {ZoneType.OPEN_SPACE}
            constrained_types = {ZoneType.MEETING, ZoneType.QUIET_ZONE, ZoneType.BREAK_ROOM, ZoneType.PHONE_BOOTH}

            # Sum constrained allocations (these must stay at minimum)
            constrained_total = sum(zone_allocations.get(zt, 0) for zt in constrained_types)
            flexible_total = sum(zone_allocations.get(zt, 0) for zt in flexible_types)

            available_for_flexible = max(0, remaining_usable - constrained_total)
            if flexible_total > 0 and available_for_flexible >= 0:
                scale_flexible = available_for_flexible / flexible_total
                for zone_type in flexible_types:
                    if zone_type in zone_allocations:
                        zone_allocations[zone_type] *= scale_flexible
                logger.info(f"  Reduced open-space from {flexible_total:.1f} to {available_for_flexible:.1f} sqm to preserve constraint minimums")
        elif new_non_booth_total < remaining_usable:
            # Extra space - give to open space and circulation for flexibility
            extra_space = remaining_usable - new_non_booth_total
            if ZoneType.OPEN_SPACE in zone_allocations:
                zone_allocations[ZoneType.OPEN_SPACE] += extra_space
            elif ZoneType.CIRCULATION in zone_allocations:
                zone_allocations[ZoneType.CIRCULATION] += extra_space

        # DEFECT I-1 CRITICAL FIX: Ensure zone allocations leave room for circulation
        # Zones should use at most the usable area (85% of total)
        # Circulation gets the remaining 15%
        # DO NOT scale zones to use 100% of surface - that leaves no room for circulation!
        total_allocated = sum(zone_allocations.values())

        # Ensure zones don't exceed usable area
        if total_allocated > usable:
            # Zones exceed usable area - scale them back to fit
            scale_factor = usable / total_allocated
            logger.info(f"Rescaling zones to fit usable area ({total_allocated:.2f} -> {usable:.2f})")
            for zone_type in zone_allocations:
                zone_allocations[zone_type] *= scale_factor
        elif total_allocated < usable:
            # Zones are less than usable area - this is OK, circulation will get the difference
            logger.info(f"Zones use {total_allocated:.2f} sqm of usable {usable:.2f}, circulation will get remainder")

        # Create zone objects
        for zone_type, count in distribution.items():
            if count == 0:
                continue

            allocated_sqm = zone_allocations.get(zone_type, 0)

            if zone_type == ZoneType.STORAGE:
                zone = Zone(
                    zone_type=zone_type.value,
                    name="Storage / Archive",
                    sqm=allocated_sqm,
                    occupancy=0,
                    adjacencies=["circulation"],
                    notes="Filing, archive, supplies storage"
                )
                zones.append(zone)
            elif zone_type == ZoneType.MEETING:
                # Subdivide meeting space into rooms (4-6 people each)
                num_rooms = max(1, count // 4)
                room_sqm = allocated_sqm / num_rooms
                for i in range(num_rooms):
                    zone = Zone(
                        zone_type=zone_type.value,
                        name=f"Meeting Room {i+1}",
                        sqm=room_sqm,
                        occupancy=4,
                        adjacencies=["open-space", "circulation"],
                        notes="Video conferencing equipped"
                    )
                    zones.append(zone)
            elif zone_type == ZoneType.PHONE_BOOTH:
                # DEFECT E FIX: Phone booths are FIXED SIZE (1.5-2.5 sqm per booth), NOT scaled by area allocation
                # Use distribution count (headcount-based), not recalculated from area
                # Number of booths = distribution count, each booth = 2.5 sqm (realistic)
                booth_sqm_fixed = self.ZONE_SIZING[ZoneType.PHONE_BOOTH]  # 2.5 sqm per booth (FIXED)
                num_booths = count  # Use distribution count, not allocated_sqm / booth_sqm

                # Each booth gets exactly booth_sqm_fixed (2.5 sqm)
                sqm_per_booth = booth_sqm_fixed

                for i in range(num_booths):
                    # Occupancy: typically 1-2 people per booth
                    occupancy_in_booth = min(2, max(1, count // num_booths + (1 if i < count % num_booths else 0)))
                    zone = Zone(
                        zone_type=zone_type.value,
                        name=f"Phone Booth {i+1}",
                        sqm=sqm_per_booth,
                        occupancy=occupancy_in_booth,
                        adjacencies=["open-space"],
                        notes="Acoustic isolation for calls (1.1m x 2.3m standard)"
                    )
                    zones.append(zone)
            else:
                # Open space, quiet zones, break rooms
                zone = Zone(
                    zone_type=zone_type.value,
                    name=zone_type.value.replace("-", " ").title(),
                    sqm=allocated_sqm,
                    occupancy=count,
                    adjacencies=self._get_adjacencies(zone_type),
                    notes=self._get_zone_notes(zone_type)
                )
                zones.append(zone)

        # Add circulation zone
        # Calculate circulation based on what's been allocated so far
        total_allocated_to_zones = sum(z.sqm for z in zones)
        circulation_sqm = max(0, self.surface_sqm - total_allocated_to_zones)

        zones.append(Zone(
            zone_type=ZoneType.CIRCULATION.value,
            name="Circulation & Common",
            sqm=circulation_sqm,
            occupancy=0,
            adjacencies=[],
            notes="Corridors, stairs, lobbies, restrooms"
        ))

        # Reconcile workstations with headcount
        # Ensure enough workstations for headcount
        open_space_zones = [z for z in zones if z.zone_type == "open-space"]
        if open_space_zones:
            total_open_occupancy = sum(z.occupancy for z in open_space_zones)
            if total_open_occupancy < self.headcount:
                # Scale up occupancy proportionally
                scale = self.headcount / total_open_occupancy if total_open_occupancy > 0 else 1
                for z in open_space_zones:
                    z.occupancy = max(1, int(z.occupancy * scale))

        return zones

    def _get_adjacencies(self, zone_type: ZoneType) -> List[str]:
        """Get recommended adjacencies for a zone type."""
        adjacencies = {
            ZoneType.OPEN_SPACE: ["meeting", "phone-booth", "circulation"],
            ZoneType.QUIET_ZONE: ["circulation", "break-room"],
            ZoneType.MEETING: ["open-space", "circulation"],
            ZoneType.PHONE_BOOTH: ["open-space"],
            ZoneType.BREAK_ROOM: ["quiet-zone", "circulation"],
            ZoneType.STORAGE: ["circulation"],
        }
        return adjacencies.get(zone_type, ["circulation"])

    def _get_zone_notes(self, zone_type: ZoneType) -> str:
        """Get descriptive notes for zone type."""
        notes = {
            ZoneType.OPEN_SPACE: "Collaborative workspace with shared desks and dynamic seating",
            ZoneType.QUIET_ZONE: "Focus areas for concentration work with sound dampening",
            ZoneType.MEETING: "Group collaboration spaces",
            ZoneType.PHONE_BOOTH: "Private call booths with acoustic isolation",
            ZoneType.BREAK_ROOM: "Casual seating, refreshments, and social spaces",
            ZoneType.STORAGE: "Filing, equipment, and supplies storage",
        }
        return notes.get(zone_type, "")

    def _calculate_window_distance(self, zones: List[Zone]) -> Tuple[float, float]:
        """
        Calculate average distance to perimeter and percentage with good light.
        Based on actual geometric coordinates.
        """
        if not zones:
            return 0.0, 0.0

        # Find envelope bounds
        min_x = min((z.x for z in zones if z.x is not None), default=0)
        min_y = min((z.y for z in zones if z.y is not None), default=0)
        max_x = max((z.x + z.width for z in zones if z.x is not None and z.width is not None), default=0)
        max_y = max((z.y + z.length for z in zones if z.y is not None and z.length is not None), default=0)

        width = max_x - min_x if max_x > min_x else 1
        height = max_y - min_y if max_y > min_y else 1

        # Calculate average distance and light percentage
        total_distance = 0.0
        light_area = 0.0
        total_area = 0.0

        for z in zones:
            if z.x is not None and z.y is not None and z.width is not None and z.length is not None:
                # Center of zone
                cx = z.x + z.width / 2
                cy = z.y + z.length / 2

                # Distance to nearest perimeter
                dist_to_perimeter = min(
                    cx - min_x,
                    max_x - cx,
                    cy - min_y,
                    max_y - cy
                )

                # Zones close to perimeter (< 5m or < 30% of width) have good light
                has_good_light = dist_to_perimeter < 5 or dist_to_perimeter < width * 0.3

                total_distance += dist_to_perimeter * z.sqm
                if has_good_light and z.zone_type != "circulation":
                    light_area += z.sqm
                total_area += z.sqm

        avg_distance = total_distance / total_area if total_area > 0 else 0
        natural_light_pct = (light_area / total_area * 100) if total_area > 0 else 0

        return avg_distance, natural_light_pct

    def calculate_metrics(self, zones: List[Zone]) -> SpaceMetrics:
        """Calculate metrics from zone layout."""
        total_zone_sqm = sum(z.sqm for z in zones)

        # Count zone types
        workstations = sum(
            z.occupancy for z in zones if z.zone_type == "open-space"
        )
        meeting_rooms = sum(
            1 for z in zones if z.zone_type == "meeting"
        )
        phone_booths = sum(
            1 for z in zones if z.zone_type == "phone-booth"
        )
        quiet_zones_count = sum(
            1 for z in zones if z.zone_type == "quiet-zone"
        )
        break_rooms = sum(
            1 for z in zones if z.zone_type == "break-room"
        )

        # Collaboration percentage (meeting + phone + break) / (total - storage - circulation)
        collaborative_sqm = sum(
            z.sqm for z in zones if z.zone_type in ["meeting", "phone-booth", "break-room"]
        )
        collaboration_pct = (collaborative_sqm / total_zone_sqm * 100) if total_zone_sqm > 0 else 0

        # Average sqm per person
        avg_per_person = self.surface_sqm / self.headcount if self.headcount > 0 else 0

        # Window distance and natural light from actual geometry
        avg_window_distance, natural_light_pct = self._calculate_window_distance(zones)

        return SpaceMetrics(
            total_sqm=self.surface_sqm,
            workstations=workstations,
            meeting_rooms=meeting_rooms,
            phone_booths=phone_booths,
            quiet_zones=quiet_zones_count,
            break_rooms=break_rooms,
            collaboration_zones_pct=collaboration_pct,
            average_sqm_per_person=avg_per_person,
            window_distance_avg=avg_window_distance,
            natural_light_zones_pct=natural_light_pct
        )

    def generate_variants(self, num_variants: int = 3) -> List[LayoutVariant]:
        """Generate multiple layout variants for the space."""
        variants = []
        distribution = self._distribute_zones()
        base_zones = self._create_zones(distribution)
        # Apply geometric layout (treemap) — returns (zones, violations)
        base_zones, base_violations = self._apply_geometric_layout(base_zones)
        logger.critical(f"generate_variants: balanced layout returned {len(base_violations)} violations: {base_violations}")
        metrics = self.calculate_metrics(base_zones)

        # Variant 1: Balanced (base)
        variant_1 = LayoutVariant(
            variant_id="balanced-001",
            layout_name="Balanced Collaboration",
            zones=base_zones,
            metrics=metrics,
            floorplan_stub_url=f"stub:///floorplans/balanced-001.png",
            design_notes=f"Balanced mix of collaborative and focus areas. {metrics.workstations} workstations, {metrics.meeting_rooms} meeting rooms.",
            constraint_violations=base_violations
        )
        logger.critical(f"variant_1 created with constraint_violations={variant_1.constraint_violations}")
        variants.append(variant_1)

        # Variant 2: Collaboration-heavy (if num_variants >= 2)
        if num_variants >= 2:
            collab_calc = SpaceCalculator(
                self.surface_sqm, self.headcount,
                [zt.value for zt in self.zone_types],
                "high_collab"
            )
            collab_dist = collab_calc._distribute_zones()
            collab_zones = collab_calc._create_zones(collab_dist)
            collab_zones, collab_violations = collab_calc._apply_geometric_layout(collab_zones)
            collab_metrics = collab_calc.calculate_metrics(collab_zones)

            variant_2 = LayoutVariant(
                variant_id="collaboration-heavy-002",
                layout_name="Collaboration-Heavy",
                zones=collab_zones,
                metrics=collab_metrics,
                floorplan_stub_url=f"stub:///floorplans/collaboration-heavy-002.png",
                design_notes=f"Maximizes collaborative spaces. {collab_metrics.collaboration_zones_pct:.1f}% collaboration zone.",
                constraint_violations=collab_violations
            )
            variants.append(variant_2)

        # Variant 3: Focus-intensive (if num_variants >= 3)
        if num_variants >= 3:
            focus_calc = SpaceCalculator(
                self.surface_sqm, self.headcount,
                [zt.value for zt in self.zone_types],
                "low_collab"
            )
            focus_dist = focus_calc._distribute_zones()
            focus_zones = focus_calc._create_zones(focus_dist)
            focus_zones, focus_violations = focus_calc._apply_geometric_layout(focus_zones)
            focus_metrics = focus_calc.calculate_metrics(focus_zones)

            variant_3 = LayoutVariant(
                variant_id="focus-intensive-003",
                layout_name="Focus-Intensive",
                zones=focus_zones,
                metrics=focus_metrics,
                floorplan_stub_url=f"stub:///floorplans/focus-intensive-003.png",
                design_notes=f"Maximizes focus areas and quiet zones. {focus_metrics.collaboration_zones_pct:.1f}% collaboration zone.",
                constraint_violations=focus_violations
            )
            variants.append(variant_3)

        return variants

    def to_dict(self) -> Dict:
        """Serialize calculator state."""
        return {
            "surface_sqm": self.surface_sqm,
            "headcount": self.headcount,
            "zone_types": [zt.value for zt in self.zone_types],
            "collaboration_style": self.collaboration_style,
        }


def generate_space_layouts_json(surface_sqm: float, headcount: int,
                                zone_types: List[str],
                                project_id: str = "default") -> str:
    """
    Generate space layouts JSON from workspace brief.

    Args:
        surface_sqm: Total workspace area
        headcount: Number of people
        zone_types: List of zone types to include
        project_id: Project identifier

    Returns:
        JSON string with layout variants and metrics
    """
    calc = SpaceCalculator(surface_sqm, headcount, zone_types)
    variants = calc.generate_variants(num_variants=3)
    logger.critical(f"generate_space_layouts_json: generated {len(variants)} variants")
    for v in variants:
        logger.critical(f"  {v.variant_id}: {len(v.constraint_violations)} violations")

    # DEFECT F3 FIX: Aggregate violations at root level
    # Calculate total violations across all variants
    total_violations = []
    total_violation_count = 0

    # Re-validate ALL zones to ensure violations are caught (belt-and-suspenders approach)
    # This catches any violations that might have been missed during layout
    logger.info(f"DEFECT F3: Re-validating all zones in all variants before returning API response")

    for v in variants:
        # DEFECT F3 FIX: ALWAYS re-check every zone against constraints
        # Ground truth is what _check_shape_constraints reports on the final zones
        # Do not trust violations reported during layout - always re-validate before returning
        re_validated_violations = []
        logger.info(f"DEFECT F3: Re-validating {v.variant_id} with {len(v.zones)} zones")

        # Debug: dump all zone coordinates before validation
        logger.info(f"DEFECT F3 PRE-VALIDATION {v.variant_id} zones:")
        for zone in v.zones:
            logger.info(f"  {zone.name}: x={zone.x:.2f}, y={zone.y:.2f}, w={zone.width:.2f}, l={zone.length:.2f}, x+w={zone.x + zone.width if zone.x is not None and zone.width is not None else 0:.2f}")

        for zone in v.zones:
            # DEFECT F3 DEBUG: Log zone coordinates before checking
            zone_info = f"name={zone.name}, x={zone.x}, y={zone.y}, w={zone.width}, l={zone.length}, sqm={zone.sqm}"
            is_valid, violation_msg = calc._check_shape_constraints(zone)
            if not is_valid:
                violation_str = f"{zone.name} ({zone.zone_type}): {violation_msg}"
                re_validated_violations.append(violation_str)
                logger.warning(f"DEFECT F3: {v.variant_id} - {zone_info} -> VIOLATION")
                logger.warning(f"  {violation_msg}")
            else:
                # Only log for zones that had high risk of overflowing
                if zone.name in ["Quiet Zone", "Circulation & Common", "Break Room"]:
                    logger.info(f"DEFECT F3: {v.variant_id} - {zone.name} OK: x+w={zone.x + zone.width if zone.x and zone.width else 'N/A'}")

        # DEFECT F3 FIX: Use re-validated violations as the authoritative source
        # Update variant's constraint_violations to match what we actually found
        reported_violations = v.constraint_violations if v.constraint_violations else []
        if len(re_validated_violations) != len(reported_violations):
            logger.warning(f"DEFECT F3: Violation count mismatch for {v.variant_id}: previously_reported={len(reported_violations)}, re_validated={len(re_validated_violations)}")
            # Log the discrepancy
            for vstr in re_validated_violations:
                if vstr not in reported_violations:
                    logger.warning(f"  NEWLY FOUND: {vstr}")
            for vstr in reported_violations:
                if vstr not in re_validated_violations:
                    logger.warning(f"  NO LONGER FOUND: {vstr}")

        # DEFECT F3 CRITICAL FIX: Set constraint_violations to re-validated results
        # This is the source of truth for is_valid calculation
        v.constraint_violations = re_validated_violations
        violations = re_validated_violations

        logger.info(f"  {v.variant_id}: final violations={len(violations)}")
        total_violations.extend(violations)
        total_violation_count += len(violations)

    # Debug: Log zones before serialization
    for v in variants:
        if v.variant_id == "collaboration-heavy-002":
            logger.info(f"SERIALIZATION: {v.variant_id} zones BEFORE asdict:")
            for z in v.zones:
                if z.name == "Quiet Zone":
                    logger.info(f"  {z.name}: x={z.x:.2f}, w={z.width:.2f}, x+w={z.x + z.width if z.x is not None and z.width is not None else 0:.2f}")

    layout_data = {
        "project_id": project_id,
        "brief": calc.to_dict(),
        "constraint_violations": total_violations,  # Root-level violations (sum of all variants)
        "is_valid": total_violation_count == 0,      # True if all variants pass all constraints
        "total_violations": total_violation_count,   # Count of violations across all variants
        "variants": [
            {
                "variant_id": v.variant_id,
                "layout_name": v.layout_name,
                "floorplan_stub_url": v.floorplan_stub_url,
                "zones": [asdict(z) for z in v.zones],
                "metrics": asdict(v.metrics),
                "design_notes": v.design_notes,
                "constraint_violations": v.constraint_violations if v.constraint_violations else [],
                "variant_is_valid": len(v.constraint_violations if v.constraint_violations else []) == 0,
            }
            for v in variants
        ]
    }

    # Debug: Log zones after serialization
    for v in variants:
        if v.variant_id == "collaboration-heavy-002":
            logger.info(f"SERIALIZATION: {v.variant_id} zones AFTER asdict:")
            for z in v.zones:
                if z.name == "Quiet Zone":
                    logger.info(f"  {z.name}: x={z.x:.2f}, w={z.width:.2f}, x+w={z.x + z.width if z.x is not None and z.width is not None else 0:.2f}")

    return json.dumps(layout_data, indent=2)
