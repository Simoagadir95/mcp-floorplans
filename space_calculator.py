"""
Deterministic space layout calculation engine.
Calculates optimal workspace configurations from brief (surface, occupants, zone types).
No API calls - pure computational logic.
"""

import json
import math
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional, Tuple
from enum import Enum


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


class SpaceCalculator:
    """Deterministic workspace layout calculator."""

    # Standard zone sizing guidelines (sqm per occupant)
    ZONE_SIZING = {
        ZoneType.OPEN_SPACE: 5.0,  # 5 sqm per workstation (hotdesking) to 8.5 sqm (dedicated)
        ZoneType.MEETING: 2.5,  # 2.5 sqm per person in meeting room
        ZoneType.PHONE_BOOTH: 2.0,  # 2 sqm single person booth
        ZoneType.QUIET_ZONE: 4.0,  # 4 sqm per person (focus area)
        ZoneType.BREAK_ROOM: 1.0,  # 1 sqm per person
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
        self.circulation_tolerance = 0.02  # ±2% surface conservation tolerance

    def _squarify_treemap(self, areas: List[Tuple[str, float]],
                          x: float, y: float, width: float, height: float,
                          rectangles: Dict[str, Tuple[float, float, float, float]],
                          row: List[Tuple[str, float]]) -> None:
        """
        Squarified treemap subdivision (Marson & Musse 2010).
        Recursively subdivides rectangular space into sub-rectangles.

        Args:
            areas: List of (zone_name, area_sqm) tuples sorted descending
            x, y: Origin coordinates (meters)
            width, height: Dimensions (meters)
            rectangles: Output dict mapping zone_name -> (x, y, width, height)
            row: Current row being processed
        """
        if not areas:
            return

        if len(areas) == 0:
            return

        # Base case: single rectangle
        if len(areas) == 1:
            name, area = areas[0]
            rectangles[name] = (x, y, width, height)
            return

        # Calculate total area of current row
        row_area = sum(a[1] for a in row) if row else 0

        # Try adding next area to row
        if not row:
            # Start new row with first area
            self._squarify_treemap(areas[1:], x, y, width, height,
                                  rectangles, [areas[0]])
        else:
            # Calculate worst aspect ratio if we add next area to row
            next_area = areas[0]
            total_area = row_area + next_area[1]

            # Decide whether to add to row or start new row
            # Use aspect ratio as metric (prefer squares)
            if len(row) == 1:
                # First in row: add next
                self._squarify_treemap(areas[1:], x, y, width, height,
                                      rectangles, row + [next_area])
            else:
                # Check if adding next would worsen aspect ratio
                current_worst = self._worst_aspect_ratio(row, width, height, row_area)
                new_worst = self._worst_aspect_ratio(row + [next_area], width, height, total_area)

                if new_worst <= current_worst:
                    # Continue row
                    self._squarify_treemap(areas[1:], x, y, width, height,
                                          rectangles, row + [next_area])
                else:
                    # Layout current row, start new one
                    new_x, new_y, new_width, new_height = self._layout_row(
                        row, x, y, width, height, row_area, rectangles
                    )
                    self._squarify_treemap(areas, new_x, new_y, new_width, new_height,
                                          rectangles, [])

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

    def _apply_geometric_layout(self, zones: List[Zone]) -> List[Zone]:
        """
        Apply squarified treemap layout to zones.
        Adds x, y, width, length coordinates to each zone.
        Ensures ALL zones receive coordinates (uses fallback row-packing for missing zones).
        """
        if not zones:
            return zones

        # Set up coordinate system: assume rectangular envelope
        # Aspect ratio 1:1 for simplicity (can be parameterized)
        side_length = math.sqrt(self.surface_sqm)

        # Apply treemap to ALL zones (including circulation)
        areas = [(z.name, z.sqm) for z in zones]
        areas.sort(key=lambda x: -x[1])  # Sort descending by area

        rectangles: Dict[str, Tuple[float, float, float, float]] = {}

        # Start squarification from top-left
        self._squarify_treemap(areas, 0, 0, side_length, side_length, rectangles, [])

        # Assign coordinates to zones from treemap
        zones_with_coords = set()
        for zone in zones:
            if zone.name in rectangles:
                x, y, w, h = rectangles[zone.name]
                zone.x = x
                zone.y = y
                zone.width = w
                zone.length = h
                zones_with_coords.add(zone.name)

        # Fallback: for zones without coordinates, use row-packing algorithm
        missing_zones = [z for z in zones if z.name not in zones_with_coords]
        if missing_zones:
            # Use deterministic row-packing for missing zones
            placements = self._row_pack_zones_deterministic(missing_zones, side_length)
            for placement in placements:
                zone_name = placement["zone_name"]
                for zone in zones:
                    if zone.name == zone_name:
                        zone.x = placement["x"]
                        zone.y = placement["y"]
                        zone.width = placement["width"]
                        zone.length = placement["height"]
                        break

        return zones

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

    def _create_zones(self, distribution: Dict[ZoneType, int]) -> List[Zone]:
        """
        Create zone objects from distribution.
        Allocates 85% of surface to usable zones, 15% to circulation.
        """
        usable = self.calculate_usable_area()  # 85% of total surface
        circulation_sqm = self.surface_sqm * self.circulation_pct  # 15% of total
        zones = []
        zone_counter = {}

        # Calculate total sqm needed for all zones
        total_calculated = 0
        zone_allocations = {}

        for zone_type, count in distribution.items():
            if count == 0:
                continue

            sqm_per_person = self.ZONE_SIZING.get(zone_type, 4.0)

            if zone_type == ZoneType.STORAGE:
                # Storage gets 5% of usable area
                zone_allocations[zone_type] = usable * 0.05
            else:
                zone_allocations[zone_type] = count * sqm_per_person
                total_calculated += zone_allocations[zone_type]

        # Normalize allocations to fit usable area (85% of total)
        # Scale all zones proportionally to fit exactly in usable area
        if total_calculated > 0 and total_calculated != usable:
            scale_factor = usable / total_calculated
            for zone_type in zone_allocations:
                if zone_type != ZoneType.STORAGE:
                    zone_allocations[zone_type] *= scale_factor

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
                # Each booth: 1 person, 2 sqm
                num_booths = count
                booth_sqm = allocated_sqm / num_booths if num_booths > 0 else 2.0
                for i in range(num_booths):
                    zone = Zone(
                        zone_type=zone_type.value,
                        name=f"Phone Booth {i+1}",
                        sqm=booth_sqm,
                        occupancy=1,
                        adjacencies=["open-space"],
                        notes="Acoustic isolation for calls"
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
        # Apply geometric layout (treemap)
        base_zones = self._apply_geometric_layout(base_zones)
        metrics = self.calculate_metrics(base_zones)

        # Variant 1: Balanced (base)
        variant_1 = LayoutVariant(
            variant_id="balanced-001",
            layout_name="Balanced Collaboration",
            zones=base_zones,
            metrics=metrics,
            floorplan_stub_url=f"stub:///floorplans/balanced-001.png",
            design_notes=f"Balanced mix of collaborative and focus areas. {metrics.workstations} workstations, {metrics.meeting_rooms} meeting rooms."
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
            collab_zones = collab_calc._apply_geometric_layout(collab_zones)
            collab_metrics = collab_calc.calculate_metrics(collab_zones)

            variant_2 = LayoutVariant(
                variant_id="collaboration-heavy-002",
                layout_name="Collaboration-Heavy",
                zones=collab_zones,
                metrics=collab_metrics,
                floorplan_stub_url=f"stub:///floorplans/collaboration-heavy-002.png",
                design_notes=f"Maximizes collaborative spaces. {collab_metrics.collaboration_zones_pct:.1f}% collaboration zone."
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
            focus_zones = focus_calc._apply_geometric_layout(focus_zones)
            focus_metrics = focus_calc.calculate_metrics(focus_zones)

            variant_3 = LayoutVariant(
                variant_id="focus-intensive-003",
                layout_name="Focus-Intensive",
                zones=focus_zones,
                metrics=focus_metrics,
                floorplan_stub_url=f"stub:///floorplans/focus-intensive-003.png",
                design_notes=f"Maximizes focus areas and quiet zones. {focus_metrics.collaboration_zones_pct:.1f}% collaboration zone."
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
            }
            for v in variants
        ]
    }

    return json.dumps(layout_data, indent=2)
