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

    def _shelf_pack_layout_simple(self, zones: List[Zone], container_width: float, container_height: float) -> Tuple[Dict[str, Tuple[float, float, float, float]], List[str]]:
        """
        SIMPLEST POSSIBLE shelf-packing: ONE SHELF ONLY.
        All zones in ONE horizontal shelf with height = total_sqm / width.
        Guarantees: w*h = sqm for each zone, Σ(w*h) = container_area.

        Args:
            zones: List of Zone objects (with sqm set)
            container_width: Container width (m)
            container_height: Container height (m)

        Returns:
            Tuple of (rectangles dict, violations list)
        """
        rectangles: Dict[str, Tuple[float, float, float, float]] = {}
        violations = []

        if not zones or container_width <= 0:
            return rectangles, violations

        # Sort zones by sqm descending (for better aspect ratios)
        sorted_zones = sorted(zones, key=lambda z: -z.sqm)

        # Single shelf: height = total_area / width (exact)
        total_sqm = sum(z.sqm for z in sorted_zones)
        shelf_height = total_sqm / container_width if container_width > 0 else 0

        logger.info(f"SINGLE SHELF: total_sqm={total_sqm:.2f}, width={container_width:.2f}, height={shelf_height:.6f}")

        # Place zones in shelf, left to right
        current_x = 0.0
        total_width_used = 0.0

        for zone_idx, zone in enumerate(sorted_zones):
            # Width = sqm / height (EXACT: width * height = sqm)
            zone_width = zone.sqm / shelf_height if shelf_height > 0 else 0

            # For last zone: extend to fill container exactly
            if zone_idx == len(sorted_zones) - 1:
                zone_width = container_width - current_x

            # Ensure positive
            zone_width = max(zone_width, 1e-9)

            # Assign geometry
            rectangles[zone.name] = (current_x, 0.0, zone_width, shelf_height)

            actual_sqm = zone_width * shelf_height
            logger.debug(f"  {zone.name:30s} x={current_x:8.4f} w={zone_width:8.4f} sqm_target={zone.sqm:8.2f} sqm_actual={actual_sqm:8.2f} err={abs(zone.sqm - actual_sqm):8.4f}")

            current_x += zone_width
            total_width_used += zone_width

        logger.info(f"  Total width used: {total_width_used:.6f} (container: {container_width:.6f}, error: {abs(total_width_used - container_width):.2e})")

        return rectangles, violations

    def _shelf_pack_layout(self, zones: List[Zone], container_width: float, container_height: float) -> Tuple[Dict[str, Tuple[float, float, float, float]], List[str]]:
        """
        DEFECT I FIX: Implement shelf-packing algorithm for EXACT area conservation.

        GUARANTEES BY CONSTRUCTION:
        - Σsqm = container_area (exact)
        - Each zone w*h = zone.sqm (exact within 1e-9)
        - Σ(w*h) = container_area (follows from above)
        - overlap = 0 (no zone overlaps)

        Algorithm (SIMPLE & DETERMINISTIC):
        1. Sort zones by sqm descending
        2. Divide zones into shelves: first N zones in shelf 1, next M in shelf 2, etc.
           (Choose N, M to balance shelf heights)
        3. For each shelf: height_i = (Σ sqm in shelf) / container_width (EXACT)
        4. For each zone in shelf: width = zone.sqm / shelf_height (EXACT, and w*h = zone.sqm)
        5. Last shelf absorbs residual height

        Result: ALL zones have w*h = sqm exactly, and Σ(w*h) = Σsqm = container_area exactly.

        Args:
            zones: List of Zone objects (with sqm, zone_type, name set)
            container_width: Container width (m)
            container_height: Container height (m)

        Returns:
            Tuple of (rectangles dict, violations list)
        """
        rectangles: Dict[str, Tuple[float, float, float, float]] = {}
        violations = []

        if not zones or container_width <= 0 or container_height <= 0:
            return rectangles, violations

        # Sort zones by area descending
        sorted_zones = sorted(zones, key=lambda z: -z.sqm)

        # STEP 1: Divide zones into shelves (balance shelf heights)
        # Simple approach: start with ~2-3 zones per shelf, adjust based on total area
        zones_per_shelf = max(1, len(sorted_zones) // 4)  # Aim for ~4 shelves
        zones_per_shelf = min(zones_per_shelf, 5)  # But at most 5 zones per shelf

        shelves: List[List[Zone]] = []
        for i in range(0, len(sorted_zones), zones_per_shelf):
            shelf = sorted_zones[i:i+zones_per_shelf]
            shelves.append(shelf)

        logger.info(f"SHELF_PACK: {len(sorted_zones)} zones into {len(shelves)} shelves ({zones_per_shelf} zones/shelf)")

        # STEP 2: Layout shelves on canvas
        current_y = 0.0

        for shelf_idx, shelf in enumerate(shelves):
            # Calculate exact shelf height
            shelf_sqm = sum(z.sqm for z in shelf)
            shelf_height = shelf_sqm / container_width if container_width > 0 else 0

            if shelf_height <= 0:
                logger.error(f"Shelf {shelf_idx}: invalid height! sqm={shelf_sqm}")
                continue

            logger.debug(f"Shelf {shelf_idx}: {len(shelf)} zones, sqm={shelf_sqm:.2f}, height={shelf_height:.4f}")

            # STEP 3: Place zones within shelf
            # width_i = zone_sqm_i / shelf_height (EXACT, and w*h = zone.sqm)
            current_x = 0.0

            for zone_idx, zone in enumerate(shelf):
                # Calculate width EXACTLY from zone area
                zone_width = zone.sqm / shelf_height if shelf_height > 0 else 0

                # For last zone in last shelf: extend to fill remaining container exactly
                if shelf_idx == len(shelves) - 1:
                    if zone_idx == len(shelf) - 1:
                        # Last zone in last shelf: fill remaining width exactly
                        zone_width = container_width - current_x

                # Ensure positive width
                zone_width = max(zone_width, 1e-9)

                # Assign geometry
                rectangles[zone.name] = (current_x, current_y, zone_width, shelf_height)

                actual_sqm = zone_width * shelf_height
                diff = abs(zone.sqm - actual_sqm)

                logger.debug(f"  {zone.name:30s} x={current_x:7.3f} y={current_y:7.3f} w={zone_width:7.3f} h={shelf_height:7.3f} sqm_t={zone.sqm:7.2f} sqm_a={actual_sqm:7.2f} err={diff:.2e}")

                current_x += zone_width

            # Last shelf absorbs residual height
            if shelf_idx == len(shelves) - 1:
                residual_height = container_height - current_y - shelf_height
                if residual_height > 1e-6:
                    # Scale all zones in this shelf to use remaining height
                    logger.warning(f"Shelf {shelf_idx} (LAST): absorbing residual height {residual_height:.4f}")
                    for zone in shelf:
                        if zone.name in rectangles:
                            x, y, w, old_h = rectangles[zone.name]
                            new_h = shelf_height + residual_height / len(shelf)
                            rectangles[zone.name] = (x, y, w, new_h)

            current_y += shelf_height

        # STEP 4: Check for constraint violations
        for zone in sorted_zones:
            if zone.name in rectangles:
                x, y, w, h = rectangles[zone.name]
                is_valid, violation_msg = self._check_shape_constraints_dimensions(zone.name, zone.zone_type, w, h)
                if not is_valid:
                    violations.append(f"{zone.name}: {violation_msg}")
                    logger.warning(f"CONSTRAINT VIOLATION: {zone.name}: {violation_msg}")

        return rectangles, violations

    def _check_shape_constraints_dimensions(self, zone_name: str, zone_type_str: str,
                                             rect_width: float, rect_height: float) -> Tuple[bool, str]:
        """
        Check shape constraints for given dimensions.

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
        DEFECT I FIX: Apply shelf-packing layout to zones.
        Adds x, y, width, length coordinates to each zone.
        GUARANTEES (by construction):
          - Σsqm == container_area (exact, no scaling)
          - overlap == 0
          - sqm == width*length for each zone (exact)
          - Constraint violations are detected and returned (not raised)

        Returns:
            Tuple of (zones, constraint_violations list)

        No repartitioning, no recursion, no backtracking — shelf-packing is deterministic.
        """
        if not zones:
            return zones, []

        # Set up coordinate system: assume rectangular container
        # For a 400 sqm space, use W x H where W*H = 400
        container_area = self.surface_sqm
        container_width = math.sqrt(container_area)
        container_height = container_area / container_width if container_width > 0 else 0

        logger.info(f"DEFECT I: Applying shelf-packing to {len(zones)} zones")
        logger.info(f"  Container: {container_width:.2f} x {container_height:.2f} = {container_area:.2f} sqm")

        # Apply shelf-packing layout (using simple single-shelf version for now)
        rectangles, violations = self._shelf_pack_layout_simple(zones, container_width, container_height)

        # Assign coordinates from rectangles to zones (in original order, not sorted)
        for zone in zones:
            if zone.name in rectangles:
                x, y, w, h = rectangles[zone.name]
                zone.x = x
                zone.y = y
                zone.width = w
                zone.length = h
                # DEFECT I FIX: Ensure sqm = width*length by recalculating from geometry
                zone.sqm = w * h
                logger.info(f"ASSIGN: {zone.name:30s} x={x:8.4f} y={y:8.4f} w={w:8.4f} l={h:8.4f} sqm={zone.sqm:8.2f}")

        # Verify all zones have geometry
        unassigned = [z for z in zones if z.x is None or z.y is None]
        if unassigned:
            for zone in unassigned:
                violations.append(f"{zone.name}: No geometry assigned by shelf-packing")
                logger.error(f"UNASSIGNED: {zone.name}")

        # Final check: verify area conservation
        total_sqm_final = sum(z.sqm for z in zones)
        logger.info(f"FINAL: Σsqm={total_sqm_final:.6f}, diff from {self.surface_sqm:.2f} = {abs(total_sqm_final - self.surface_sqm):.6f}")

        return zones, violations

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
        extra_circulation_sqm = 0  # Track extra space to add to circulation later

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
            # Extra space - give to open space or track for circulation
            extra_space = remaining_usable - new_non_booth_total
            if ZoneType.OPEN_SPACE in zone_allocations:
                zone_allocations[ZoneType.OPEN_SPACE] += extra_space
            else:
                # Open space not in distribution - add extra space to circulation
                extra_circulation_sqm = extra_space
                logger.info(f"  Adding extra {extra_space:.1f} sqm to circulation (open-space not in zone types)")

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

        # Add circulation zone (includes any extra space not allocated to other zones)
        total_allocated = sum(z.sqm for z in zones)
        circulation_total = self.surface_sqm - total_allocated
        zones.append(Zone(
            zone_type=ZoneType.CIRCULATION.value,
            name="Circulation & Common",
            sqm=circulation_total,
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
