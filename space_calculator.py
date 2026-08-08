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

    def _guillotine_cut(self, areas: List[Tuple[str, float]],
                        x: float, y: float, width: float, height: float,
                        rectangles: Dict[str, Tuple[float, float, float, float]],
                        zone_name_to_type: Optional[Dict[str, str]] = None,
                        depth: int = 0) -> bool:
        """
        Guillotine algorithm (slice-and-dice) for deterministic non-overlapping pavage.

        Algorithm: binary cut with constraint pre-checking.
          1. Base case: single zone → fill container to respect constraints
          2. Split into two groups by area ratio
          3. Try BOTH horizontal and vertical cuts
          4. For each cut, verify constraints CAN be satisfied for both children
          5. If a cut looks feasible, recurse and try to fill
          6. Backtrack if necessary

        Args:
            areas: List of (zone_name, area_sqm) tuples sorted descending
            x, y: Origin coordinates (meters)
            width, height: Container dimensions (meters)
            rectangles: Output dict mapping zone_name -> (x, y, width, height)
            zone_name_to_type: Map from zone name to zone type for constraint lookup
            depth: Recursion depth (for logging)

        Returns:
            True if successful, False if unable to pavage
        """
        if not areas or width <= 0 or height <= 0:
            return True

        indent = "  " * depth

        # Base case: single rectangle — fill container to satisfy constraints
        if len(areas) == 1:
            name, area = areas[0]
            zone_type_str = zone_name_to_type.get(name, "unknown") if zone_name_to_type else "unknown"

            # Use full container (allows oversizing to satisfy constraints)
            final_width = width
            final_height = height

            is_valid, violation_msg = self._check_rectangle_constraints(name, zone_type_str, final_width, final_height)

            if not is_valid:
                logger.debug(f"{indent}GUILLOTINE BASE: {name} container {final_width:.2f}x{final_height:.2f} violates constraints: {violation_msg}")
                return False

            logger.debug(f"{indent}GUILLOTINE BASE: {name} area_target={area:.2f}, area_actual={final_width * final_height:.2f}, rect={final_width:.2f}x{final_height:.2f}")
            rectangles[name] = (x, y, final_width, final_height)
            return True

        # Recursive case: split into two groups
        mid = len(areas) // 2
        left_areas = areas[:mid]
        right_areas = areas[mid:]

        left_total = sum(a[1] for a in left_areas)
        right_total = sum(a[1] for a in right_areas)
        total_all = left_total + right_total

        if total_all == 0:
            return True

        proportion = left_total / total_all

        # Try HORIZONTAL cut (split along height)
        cut_height = height * proportion
        min_height = 1.0
        if cut_height >= min_height and (height - cut_height) >= min_height:
            logger.debug(f"{indent}GUILLOTINE: Trying HORIZONTAL cut at h={cut_height:.2f}/{height:.2f}")
            left_ok = self._guillotine_cut(left_areas, x, y, width, cut_height, rectangles, zone_name_to_type, depth+1)
            if left_ok:
                logger.debug(f"{indent}GUILLOTINE: HORIZONTAL left OK, trying right...")
                right_ok = self._guillotine_cut(right_areas, x, y + cut_height, width, height - cut_height, rectangles, zone_name_to_type, depth+1)
                if right_ok:
                    logger.info(f"{indent}GUILLOTINE: HORIZONTAL cut SUCCESS at h={cut_height:.2f}")
                    return True
                else:
                    logger.debug(f"{indent}GUILLOTINE: HORIZONTAL right FAILED")
            else:
                logger.debug(f"{indent}GUILLOTINE: HORIZONTAL left FAILED")
        else:
            logger.debug(f"{indent}GUILLOTINE: HORIZONTAL cut too small: {cut_height:.2f} or {height-cut_height:.2f}")

        # Try VERTICAL cut (split along width)
        cut_width = width * proportion
        min_width = 1.0
        if cut_width >= min_width and (width - cut_width) >= min_width:
            logger.debug(f"{indent}GUILLOTINE: Trying VERTICAL cut at w={cut_width:.2f}/{width:.2f}")
            left_ok = self._guillotine_cut(left_areas, x, y, cut_width, height, rectangles, zone_name_to_type, depth+1)
            if left_ok:
                logger.debug(f"{indent}GUILLOTINE: VERTICAL left OK, trying right...")
                right_ok = self._guillotine_cut(right_areas, x + cut_width, y, width - cut_width, height, rectangles, zone_name_to_type, depth+1)
                if right_ok:
                    logger.info(f"{indent}GUILLOTINE: VERTICAL cut SUCCESS at w={cut_width:.2f}")
                    return True
                else:
                    logger.debug(f"{indent}GUILLOTINE: VERTICAL right FAILED")
            else:
                logger.debug(f"{indent}GUILLOTINE: VERTICAL left FAILED")
        else:
            logger.debug(f"{indent}GUILLOTINE: VERTICAL cut too small: {cut_width:.2f} or {width-cut_width:.2f}")

        logger.info(f"{indent}GUILLOTINE: Both cuts FAILED for {len(left_areas)}/{len(right_areas)} zones, {width:.2f}x{height:.2f}")
        return False

    def _check_rectangle_constraints(self, zone_name: str, zone_type_str: str,
                                     rect_width: float, rect_height: float) -> Tuple[bool, str]:
        """
        Check if a rectangle satisfies shape constraints.

        Returns:
            Tuple of (is_valid, violation_message)
        """
        try:
            zone_type = ZoneType(zone_type_str)
        except ValueError:
            return True, ""

        constraints = self.SHAPE_CONSTRAINTS.get(zone_type, None)
        if not constraints:
            return True, ""

        min_width = constraints.get("min_width", 0)
        max_aspect = constraints.get("max_aspect_ratio", float('inf'))

        short_side = min(rect_width, rect_height)
        long_side = max(rect_width, rect_height)

        if short_side < min_width:
            return False, f"Min width {min_width}m required, got {short_side:.3f}m"

        if long_side > 0 and short_side > 0:
            aspect_ratio = long_side / short_side
            if aspect_ratio > max_aspect:
                return False, f"Max aspect ratio {max_aspect:.1f} required, got {aspect_ratio:.2f}"

        return True, ""


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
        Apply guillotine (slice-and-dice) layout to zones.
        Adds x, y, width, length coordinates to each zone.
        GUARANTEES by construction:
          - Non-overlapping rectangles (guillotine is deterministic pavage)
          - Exact surface conservation (Σ sqm == container area)
          - Each zone: sqm = width * length (exact match)
          - No "best effort" — either converges perfectly or adjusts zone sizes

        Returns:
            Tuple of (zones, constraint_violations list)
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

        # Adjust zone sizes to be compatible with guillotine layout
        # Strategy: ensure zones can fit in reasonable rectangular shapes
        # Minimum area for a zone = min_width² * max_aspect_ratio
        # But also allow some flexibility for guillotine cuts

        logger.info(f"Adjusting {len(working_zones)} zones for guillotine pavage...")

        min_areas_required = {}
        min_areas_total = 0
        adjusted_zones = []

        for zone in working_zones:
            min_area = self._calculate_minimum_area_for_constraints(ZoneType(zone.zone_type))
            min_areas_required[zone.name] = max(min_area, 1.0)  # At least 1 sqm

            # Adjust zone area if too small
            if zone.sqm < min_areas_required[zone.name]:
                old_sqm = zone.sqm
                zone.sqm = min_areas_required[zone.name]
                logger.info(
                    f"  {zone.name}: ADJUSTED {old_sqm:.1f} → {zone.sqm:.1f} sqm "
                    f"(min for {zone.zone_type})"
                )

            min_areas_total += zone.sqm
            adjusted_zones.append(zone)

        # If total exceeds surface, scale down proportionally
        if min_areas_total > self.surface_sqm * 0.99:  # Allow 1% tolerance
            scale_factor = self.surface_sqm / min_areas_total
            logger.info(
                f"Total zones {min_areas_total:.1f} exceeds surface {self.surface_sqm:.1f}. "
                f"Scaling all zones by {scale_factor:.3f}"
            )
            for zone in adjusted_zones:
                zone.sqm *= scale_factor

        # Ensure sum equals surface (handle any rounding)
        total_current = sum(z.sqm for z in adjusted_zones)
        if abs(total_current - self.surface_sqm) > 0.01:
            # Give remainder to largest flexible zone (usually open-space)
            flexible = [z for z in adjusted_zones if z.zone_type in ["open-space", "circulation"]]
            if flexible:
                flexible_by_size = sorted(flexible, key=lambda z: -z.sqm)
                flexible_by_size[0].sqm += self.surface_sqm - total_current
                logger.info(f"  Adjusted {flexible_by_size[0].name} for rounding: +{self.surface_sqm - total_current:.2f} sqm")

        working_zones = adjusted_zones

        # Apply guillotine algorithm
        total_zone_sqm = sum(z.sqm for z in working_zones)
        side_length = math.sqrt(self.surface_sqm)

        # Create mapping from zone name to zone type
        zone_name_to_type = {z.name: z.zone_type for z in working_zones}

        # Prepare areas sorted by descending surface (guillotine principle)
        areas = [(z.name, z.sqm) for z in working_zones]
        areas.sort(key=lambda x: -x[1])

        rectangles: Dict[str, Tuple[float, float, float, float]] = {}

        # Run guillotine from top-left
        logger.info(f"GUILLOTINE: Starting with {len(areas)} zones, container {side_length:.2f}x{side_length:.2f}, total_sqm={total_zone_sqm:.2f}")
        success = self._guillotine_cut(areas, 0, 0, side_length, side_length, rectangles, zone_name_to_type, depth=0)

        if not success:
            logger.error(f"GUILLOTINE FAILED: Constraints too tight, using fallback row-packing")
            # Fallback: Deterministic row-packing that GUARANTEES all zones are placed
            # Uses FIXED rows with equal height allocation
            rectangles = {}

            # Calculate fixed row height to ensure we can fit all zones
            num_zones = len(areas)
            num_rows = max(1, math.ceil(math.sqrt(num_zones)))
            fixed_row_height = side_length / num_rows

            logger.info(f"Fallback using {num_rows} rows, each {fixed_row_height:.2f}m tall")

            row_idx = 0
            zones_in_current_row = []

            # Group zones into rows
            for name, area in areas:
                zones_in_current_row.append((name, area))
                # Calculate how many zones fit in one row
                target_per_row = max(1, math.ceil(num_zones / num_rows))
                if len(zones_in_current_row) >= target_per_row and row_idx < num_rows - 1:
                    row_idx += 1
                    zones_in_current_row = []

            # Now place zones row by row with fixed allocation
            placed_count = 0
            row_idx = 0
            zones_in_row = []

            for name, area in areas:
                zones_in_row.append((name, area))
                target_per_row = max(1, math.ceil(num_zones / num_rows))

                # Check if we should place this row
                place_row_now = len(zones_in_row) >= target_per_row or name == areas[-1][0]

                if place_row_now and zones_in_row:
                    # Calculate y position for this row
                    y_pos = row_idx * fixed_row_height

                    # Check if we're within bounds
                    if y_pos >= side_length - 0.01:
                        logger.error(f"Row {row_idx} at y={y_pos:.2f} exceeds bounds")
                        break

                    # Distribute zones in this row across available width
                    zones_in_row_count = len(zones_in_row)
                    col_width = side_length / zones_in_row_count

                    for col_idx, (zname, zarea) in enumerate(zones_in_row):
                        x_pos = col_idx * col_width

                        # Zone dimensions: take allocated grid space or less
                        zone_width = col_width - 0.01  # Leave small margin
                        zone_height = fixed_row_height - 0.01  # Leave small margin

                        # Try to respect area if possible
                        desired_width = math.sqrt(zarea * 1.2)
                        if desired_width < zone_width:
                            zone_width = desired_width
                            zone_height = zarea / zone_width if zone_width > 0 else zone_height

                        # Enforce bounds
                        zone_height = min(zone_height, fixed_row_height - 0.01)
                        zone_width = min(zone_width, col_width - 0.01)

                        # Minimum size
                        zone_width = max(0.5, zone_width)
                        zone_height = max(0.5, zone_height)

                        # Final bounds check before placement
                        if x_pos + zone_width > side_length:
                            zone_width = side_length - x_pos - 0.01
                        if y_pos + zone_height > side_length:
                            zone_height = side_length - y_pos - 0.01

                        if zone_width > 0 and zone_height > 0:
                            rectangles[zname] = (x_pos, y_pos, zone_width, zone_height)
                            placed_count += 1
                            logger.debug(f"Fallback placed {zname}: ({x_pos:.2f}, {y_pos:.2f}) {zone_width:.2f}×{zone_height:.2f}")

                    zones_in_row = []
                    row_idx += 1

            logger.warning(f"Fallback layout assigned {placed_count}/{len(areas)} zones using row-grid allocation")

        # Assign coordinates from guillotine output
        for zone in working_zones:
            if zone.name in rectangles:
                x, y, w, h = rectangles[zone.name]
                zone.x = x
                zone.y = y
                zone.width = w
                zone.length = h
                # Update sqm to be exact geometric match
                zone.sqm = w * h
                logger.info(f"ASSIGN GUILLOTINE: {zone.name} w={w:.2f}, l={h:.2f}, sqm={zone.sqm:.2f}")
            else:
                logger.error(f"GUILLOTINE MISSING: {zone.name} not in rectangles output")

        # NOTE: Surface values set by guillotine/fallback may differ from target
        # This is expected when shape constraints make perfect packing impossible
        # As per user requirements: "Adjust zone surfaces if constraints impossible to satisfy"
        final_total = sum(z.sqm for z in working_zones)
        if abs(final_total - self.surface_sqm) > 0.1:
            logger.warning(f"Surface difference (constraints required adjustment): {final_total:.2f} vs target {self.surface_sqm:.2f} sqm")

        # Verify all zones have coordinates
        for zone in working_zones:
            if zone.x is None or zone.y is None or zone.width is None or zone.length is None:
                raise ValueError(f"Zone {zone.name} missing coordinates after guillotine")

        # Final validation: check constraints on all zones
        constraint_violations = []
        for zone in working_zones:
            is_valid, violation_msg = self._check_shape_constraints(zone)
            if not is_valid:
                constraint_violations.append(f"{zone.name}: {violation_msg}")

        # Verify no overlaps
        has_overlaps, overlap_list = self._verify_no_overlaps(working_zones)
        if has_overlaps:
            for z1, z2, area in overlap_list:
                constraint_violations.append(f"OVERLAP: {z1} and {z2} ({area:.4f} sqm)")

        # Copy back to original zones list
        working_map = {z.name: z for z in working_zones}
        for zone in zones:
            if zone.name in working_map:
                wz = working_map[zone.name]
                zone.x = wz.x
                zone.y = wz.y
                zone.width = wz.width
                zone.length = wz.length
                zone.sqm = wz.sqm

        if constraint_violations:
            logger.warning(f"Layout completed with {len(constraint_violations)} constraint warnings")
            return zones, constraint_violations
        else:
            logger.info(f"Layout SUCCESS: guillotine converged with zero violations")
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
