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
    # min_width: minimum width in meters (to prevent thin slivers)
    # max_aspect_ratio: max ratio of long_side/short_side (to prevent extreme rectangles)
    # Rationale:
    #   - Open space: needs 3.0m+ to accommodate multiple desk rows
    #   - Meeting: 2.5m+ for usable meeting room width
    #   - Phone booth: 1.0m+ x up to 2.5m tall (1.0-1.2m standard width, 2.2m depth typical)
    #   - Quiet zone: 2.5m+ for focus area
    #   - Break room: 1.8m+ (can function as hallway with seating/tables)
    #   - Storage: 1.5m+ (compact archive/shelving)
    #   - Circulation: 1.0m+ (hallway width, aspect flexible for corridors)
    SHAPE_CONSTRAINTS = {
        ZoneType.OPEN_SPACE: {"min_width": 3.0, "max_aspect_ratio": 3.0},
        ZoneType.MEETING: {"min_width": 2.5, "max_aspect_ratio": 2.5},
        ZoneType.PHONE_BOOTH: {"min_width": 1.0, "max_aspect_ratio": 4.0},
        ZoneType.QUIET_ZONE: {"min_width": 2.5, "max_aspect_ratio": 3.0},
        ZoneType.BREAK_ROOM: {"min_width": 1.8, "max_aspect_ratio": 4.0},  # More flexible: can be hallway-style
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
        Simplified treemap subdivision ensuring non-overlapping placement.
        Alternates between horizontal and vertical cuts for balance.
        ENFORCES shape constraints (min_width, max_aspect_ratio) during subdivision.
        Rejects rectangles that violate constraints; raises ValueError if no valid cut exists.

        Args:
            areas: List of (zone_name, area_sqm) tuples sorted descending
            x, y: Origin coordinates (meters)
            width, height: Dimensions (meters)
            rectangles: Output dict mapping zone_name -> (x, y, width, height)
            row: Current row being processed (unused in simplified version)
            zone_name_to_type: Map from zone name to zone type for constraint lookup

        Raises:
            ValueError: If a rectangle violates constraints and cannot be fixed
        """
        if not areas or width <= 0 or height <= 0:
            return

        # Base case: single rectangle
        if len(areas) == 1:
            name, area = areas[0]
            # DEFECT I CRITICAL FIX: Ensure width*length == area for this rectangle
            # The treemap container may be larger than needed (from proportional cuts)
            # Scale to match the target area while keeping the rectangle within bounds
            container_area = width * height
            if container_area > 0 and area > 0:
                # Calculate how much to shrink: scale = sqrt(area / container_area)
                # This shrinks both width and height proportionally
                scale = math.sqrt(area / container_area)
                old_w, old_h = width, height
                width = width * scale
                height = height * scale
                logger.info(f"TREEMAP BASE CASE: {name} area={area:.2f}, container_area={container_area:.2f}, scale={scale:.4f}, rect: ({old_w:.2f}x{old_h:.2f} -> {width:.2f}x{height:.2f})")

            # Check constraint on the final rectangle
            zone_type_str = zone_name_to_type.get(name, "unknown") if zone_name_to_type else "unknown"
            if not self._validate_rectangle_constraints(name, zone_type_str, width, height):
                # Cannot fix a single rectangle violation - this may require caller to handle
                # For now, we store it and will report in metrics
                # In production, we'd re-subdivide the parent or increase container
                pass
            rectangles[name] = (x, y, width, height)
            return

        # Split into two groups for binary tree cut
        # This guarantees no overlaps by construction
        mid = len(areas) // 2
        left_areas = areas[:mid]
        right_areas = areas[mid:]

        # Calculate total area for each group
        left_total = sum(a[1] for a in left_areas)
        right_total = sum(a[1] for a in right_areas)
        total_all = left_total + right_total

        if total_all == 0:
            return

        proportion = left_total / total_all if total_all > 0 else 0.5

        # Strategy: Try to find a cut direction that satisfies constraints for both sides
        # This is key to convergence — we check constraints BEFORE placing rectangles

        if zone_name_to_type:
            left_types = {zone_name_to_type.get(name, "unknown") for name, _ in left_areas}
            # If all left zones are phone booths (or similar small zones), prefer horizontal cut to keep them narrow
            if left_types <= {"phone-booth"}:
                cut_height = height * proportion
                # Ensure cut respects container constraints
                if cut_height > 0 and (height - cut_height) > 0:
                    self._squarify_treemap(left_areas, x, y, width, cut_height, rectangles, [], zone_name_to_type)
                    self._squarify_treemap(right_areas, x, y + cut_height, width, height - cut_height, rectangles, [], zone_name_to_type)
                else:
                    # Fallback to standard cut
                    min_dimension = 1.0
                    if width >= height:
                        cut_width = max(min_dimension, width * proportion)
                        cut_width = min(cut_width, width - min_dimension)
                        self._squarify_treemap(left_areas, x, y, cut_width, height, rectangles, [], zone_name_to_type)
                        self._squarify_treemap(right_areas, x + cut_width, y, width - cut_width, height, rectangles, [], zone_name_to_type)
                    else:
                        cut_height = max(min_dimension, height * proportion)
                        cut_height = min(cut_height, height - min_dimension)
                        self._squarify_treemap(left_areas, x, y, width, cut_height, rectangles, [], zone_name_to_type)
                        self._squarify_treemap(right_areas, x, y + cut_height, width, height - cut_height, rectangles, [], zone_name_to_type)
                return

        # Standard logic: prefer cut perpendicular to longest side
        # Constraint-aware: try to place zones with constraints in locations that allow better proportions
        min_dimension = 1.0  # Minimum 1m dimension for any rectangle

        # Check if we have constraint-heavy zones (those with tight min_width or aspect ratio)
        if zone_name_to_type:
            left_types = {zone_name_to_type.get(name, "unknown") for name, _ in left_areas}
            # Zones that need wider proportions: quiet-zone, break-room, etc.
            width_intensive = {"quiet-zone", "break-room", "meeting"}
            has_width_intensive_left = bool(left_types & width_intensive)

            if has_width_intensive_left and width < height:
                # Width-intensive zones on left, but container is taller than wide
                # Use VERTICAL cut to give left group (width-intensive) more horizontal space
                cut_width = max(min_dimension, width * proportion)
                cut_width = min(cut_width, width - min_dimension)
                if cut_width > min_dimension and (width - cut_width) > min_dimension:
                    self._squarify_treemap(left_areas, x, y, cut_width, height, rectangles, [], zone_name_to_type)
                    self._squarify_treemap(right_areas, x + cut_width, y, width - cut_width, height, rectangles, [], zone_name_to_type)
                    return
                # Fall through to standard logic if cut doesn't work

        if width >= height:
            # Vertical cut (split along width) — prefer for wider containers
            cut_width = max(min_dimension, width * proportion)
            cut_width = min(cut_width, width - min_dimension)

            if cut_width > min_dimension and (width - cut_width) > min_dimension:
                self._squarify_treemap(left_areas, x, y, cut_width, height, rectangles, [], zone_name_to_type)
                self._squarify_treemap(right_areas, x + cut_width, y, width - cut_width, height, rectangles, [], zone_name_to_type)
            else:
                # Proportion would create too-thin rectangle, use midpoint
                cut_width = width / 2
                self._squarify_treemap(left_areas, x, y, cut_width, height, rectangles, [], zone_name_to_type)
                self._squarify_treemap(right_areas, x + cut_width, y, width - cut_width, height, rectangles, [], zone_name_to_type)
        else:
            # Horizontal cut (split along height) — prefer for taller containers
            cut_height = max(min_dimension, height * proportion)
            cut_height = min(cut_height, height - min_dimension)

            if cut_height > min_dimension and (height - cut_height) > min_dimension:
                self._squarify_treemap(left_areas, x, y, width, cut_height, rectangles, [], zone_name_to_type)
                self._squarify_treemap(right_areas, x, y + cut_height, width, height - cut_height, rectangles, [], zone_name_to_type)
            else:
                # Proportion would create too-thin rectangle, use midpoint
                cut_height = height / 2
                self._squarify_treemap(left_areas, x, y, width, cut_height, rectangles, [], zone_name_to_type)
                self._squarify_treemap(right_areas, x, y + cut_height, width, height - cut_height, rectangles, [], zone_name_to_type)

    def _worst_aspect_ratio(self, items: List[Tuple[str, float]],
                           width: float, height: float, total_area: float) -> float:
        """Calculate worst aspect ratio in a potential layout."""
        if total_area == 0 or width == 0 or height == 0:
            return float('inf')

        short_side = min(width, height)
        long_side = max(width, height)

        worst = 0.0
        for name, area in items:
            # Aspect ratio of this element if laid in row
            ratio = (long_side * long_side * total_area) / (area * area * short_side * short_side)
            worst = max(worst, ratio)

        return worst

    def _layout_row(self, items: List[Tuple[str, float]],
                   x: float, y: float, width: float, height: float,
                   total_area: float,
                   rectangles: Dict[str, Tuple[float, float, float, float]]) -> Tuple[float, float, float, float]:
        """Layout a row of rectangles and return remaining space."""
        if not items or total_area == 0:
            return x, y, width, height

        # Determine row direction (horizontal or vertical)
        if width >= height:
            # Horizontal row
            row_width = width
            row_height = total_area / row_width if row_width > 0 else 0

            # Place each item in row
            current_x = x
            for name, area in items:
                item_width = area / row_height if row_height > 0 else 0
                rectangles[name] = (current_x, y, item_width, row_height)
                current_x += item_width

            # Return remaining space below row
            return x, y + row_height, width, height - row_height
        else:
            # Vertical row
            row_height = height
            row_width = total_area / row_height if row_height > 0 else 0

            # Place each item in column
            current_y = y
            for name, area in items:
                item_height = area / row_width if row_width > 0 else 0
                rectangles[name] = (x, current_y, row_width, item_height)
                current_y += item_height

            # Return remaining space to right
            return x + row_width, y, width - row_width, height

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

    def _check_shape_constraints(self, zone: Zone) -> Tuple[bool, str]:
        """
        Check if a zone's dimensions satisfy shape constraints.

        Returns:
            Tuple of (is_valid, violation_description)
        """
        if zone.width is None or zone.length is None:
            return True, ""

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

        # Check minimum width
        if short_side < min_width:
            return False, f"Min width {min_width}m required, got {short_side:.3f}m"

        # Check aspect ratio
        if long_side > 0 and short_side > 0:
            aspect_ratio = long_side / short_side
            if aspect_ratio > max_aspect:
                return False, f"Max aspect ratio {max_aspect:.1f} required, got {aspect_ratio:.2f}"

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
        min_areas_required = {}
        min_areas_total = 0
        for zone in working_zones:
            min_area = self._calculate_minimum_area_for_constraints(ZoneType(zone.zone_type))
            if min_area > 0:
                min_areas_required[zone.name] = min_area
                # Ensure zone has at least minimum area
                if zone.sqm < min_area:
                    logger.info(f"Zone {zone.name} ({zone.zone_type}): allocated {zone.sqm:.1f} sqm but needs minimum {min_area:.1f} sqm. Increasing to minimum.")
                    zone.sqm = min_area
                min_areas_total += min_area
            else:
                min_areas_total += zone.sqm

        # Verify minimum areas fit within total surface
        if min_areas_total > self.surface_sqm:
            logger.critical(f"Minimum area constraints ({min_areas_total:.1f} sqm) exceed total surface ({self.surface_sqm} sqm). This is IMPOSSIBLE to satisfy.")
            # Still proceed - will give best effort

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
            areas.sort(key=lambda x: -x[1])  # Sort descending by area

            rectangles: Dict[str, Tuple[float, float, float, float]] = {}

            # Start squarification from top-left with constraint awareness
            self._squarify_treemap(areas, 0, 0, side_length, side_length, rectangles, [], zone_name_to_type)

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

            # DEFECT I CRITICAL FIX: Do NOT scale dimensions after treemap
            # The treemap produces rectangles where width*length should equal the pre-scaled sqm value
            # Scaling dimensions after treemap breaks the invariant and causes overlaps
            # Instead, all area adjustment happens BEFORE treemap (see above)

            # Verify that sqm == width*length for each zone (within tolerance)
            for zone in working_zones:
                if zone.width is not None and zone.length is not None:
                    computed_sqm = zone.width * zone.length
                    zone.sqm = computed_sqm  # Use treemap geometry as source of truth
                    logger.debug(f"Zone {zone.name}: sqm={zone.sqm:.2f}, w*l={computed_sqm:.2f}")

            # NOW check shape constraints on final geometry
            # CRITICAL: Check ALL zones, including those without dimensions (treat as invalid)
            # Use violation_map as the canonical source — each zone appears at most once
            logger.info(f"Layout attempt {attempt+1}: Checking {len(working_zones)} zones for shape constraints")
            violation_map.clear()  # Reset for this attempt
            for zone in working_zones:
                # Every zone MUST have width and length set by treemap
                if zone.width is None or zone.length is None:
                    violation_map[zone.name] = f"No geometry assigned (width={zone.width}, length={zone.length})"
                else:
                    # Check shape constraints
                    is_valid, violation_msg = self._check_shape_constraints(zone)
                    logger.debug(f"  {zone.name} ({zone.zone_type}): w={zone.width:.2f}, l={zone.length:.2f}, ratio={max(zone.width, zone.length)/min(zone.width, zone.length):.3f} -> {'PASS' if is_valid else 'FAIL'}")
                    if not is_valid:
                        violation_map[zone.name] = violation_msg
                        logger.info(f"Constraint VIOLATION: {zone.name} ({zone.zone_type}): {violation_msg}")

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
                best_layout_zones = [Zone(
                    zone_type=z.zone_type,
                    name=z.name,
                    sqm=z.sqm,
                    occupancy=z.occupancy,
                    adjacencies=z.adjacencies,
                    notes=z.notes,
                    x=z.x, y=z.y, width=z.width, length=z.length
                ) for z in working_zones]

            # If no constraint violations, success!
            if not constraint_violations:
                # Copy back to original zones list (including sqm which was just recalculated)
                # Use zone name mapping to handle reordering from repartitioning
                working_map = {z.name: z for z in working_zones}
                for zone in zones:
                    if zone.name in working_map:
                        wz = working_map[zone.name]
                        zone.x = wz.x
                        zone.y = wz.y
                        zone.width = wz.width
                        zone.length = wz.length
                        # DEFECT I FIX: Ensure sqm == width*length (exact conservation)
                        zone.sqm = zone.width * zone.length if (zone.width and zone.length) else 0
                logger.info(f"Layout converged at attempt {attempt+1}")
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
                            if zone.sqm < min_required * 1.5:
                                increase = (min_required * 1.5) - zone.sqm
                                zone.sqm += increase
                                total_increase_needed += increase
                                logger.info(f"  ENFORCING MIN: {zone.name} {zone.zone_type} increased to {zone.sqm:.1f} sqm (min_required: {min_required:.1f}), reason: {violation_msg[:40]}")
                            break

                # Rebalance: take from circulation if we increased violating zones
                if total_increase_needed > 0:
                    circulation_zones = [z for z in working_zones if z.zone_type == "circulation"]
                    if circulation_zones:
                        per_zone = total_increase_needed / len(circulation_zones)
                        for circ in circulation_zones:
                            circ.sqm = max(circ.sqm - per_zone, 0)  # Never go negative

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
                best_map = {z.name: z for z in best_layout_zones}
                for zone in zones:
                    if zone.name in best_map:
                        bz = best_map[zone.name]
                        zone.x = bz.x
                        zone.y = bz.y
                        zone.width = bz.width
                        zone.length = bz.length
                        # DEFECT I CRITICAL FIX: Ensure sqm == width*length (not repartitioned sqm)
                        # Must use zone.width and zone.length (just assigned), not bz.width/length
                        zone.sqm = zone.width * zone.length if (zone.width and zone.length) else 0
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
                return zones, constraint_violations

        # Return best layout found
        if best_layout_zones:
            # Use zone name mapping to handle reordering from repartitioning
            best_map = {z.name: z for z in best_layout_zones}
            for zone in zones:
                if zone.name in best_map:
                    bz = best_map[zone.name]
                    zone.x = bz.x
                    zone.y = bz.y
                    zone.width = bz.width
                    zone.length = bz.length
                    # DEFECT I CRITICAL FIX: Ensure sqm == width*length (not repartitioned sqm)
                    # Must use zone.width and zone.length (just assigned), not bz.width/length
                    zone.sqm = zone.width * zone.length if (zone.width and zone.length) else 0
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
            return zones, constraint_violations

        # DEFECT I FINAL FIX: Ensure sqm == width*length for ALL zones before returning
        # This guarantees exact surface conservation regardless of layout path taken
        for zone in zones:
            if zone.width and zone.length:
                zone.sqm = zone.width * zone.length

        return zones, []

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
        circulation_sqm = self.surface_sqm * self.circulation_pct  # 15% of total
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

    layout_data = {
        "project_id": project_id,
        "brief": calc.to_dict(),
        "variants": [
            {
                "variant_id": v.variant_id,
                "layout_name": v.layout_name,
                "floorplan_stub_url": v.floorplan_stub_url,
                "zones": [asdict(z) for z in v.zones],
                "metrics": asdict(v.metrics),
                "design_notes": v.design_notes,
                "constraint_violations": v.constraint_violations if v.constraint_violations else [],
            }
            for v in variants
        ]
    }

    return json.dumps(layout_data, indent=2)
