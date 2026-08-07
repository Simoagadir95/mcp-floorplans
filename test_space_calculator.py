"""
Unit tests for space_calculator.py

Tests deterministic space layout calculation logic.
"""

import json
import pytest
from space_calculator import (
    SpaceCalculator,
    generate_space_layouts_json,
    ZoneType,
    SpaceMetrics
)


class TestSpaceCalculator:
    """Test suite for SpaceCalculator."""

    def test_calculator_initialization(self):
        """Test basic initialization."""
        calc = SpaceCalculator(
            surface_sqm=200,
            headcount=20,
            zone_types=["open-space", "meeting"]
        )
        assert calc.surface_sqm == 200
        assert calc.headcount == 20
        assert len(calc.zone_types) == 2

    def test_calculate_usable_area(self):
        """Test usable area calculation (85% of total after circulation)."""
        calc = SpaceCalculator(200, 20, [])
        usable = calc.calculate_usable_area()
        assert usable == pytest.approx(200 * 0.85, rel=1e-2)

    def test_zone_distribution(self):
        """Test zone distribution across zone types."""
        calc = SpaceCalculator(
            surface_sqm=200,
            headcount=20,
            zone_types=["open-space", "meeting", "quiet-zone"]
        )
        distribution = calc._distribute_zones()

        # Should have open space and quiet zones distributed
        assert ZoneType.OPEN_SPACE in distribution
        assert distribution[ZoneType.OPEN_SPACE] > 0

    def test_metrics_calculation(self):
        """Test metrics calculation from zones."""
        calc = SpaceCalculator(200, 20, ["open-space", "meeting", "quiet-zone"])
        distribution = calc._distribute_zones()
        zones = calc._create_zones(distribution)
        metrics = calc.calculate_metrics(zones)

        # Verify metric types and ranges
        assert isinstance(metrics, SpaceMetrics)
        assert metrics.total_sqm == 200
        assert metrics.average_sqm_per_person == pytest.approx(200/20, rel=1e-2)
        assert 0 <= metrics.collaboration_zones_pct <= 100
        assert 0 <= metrics.natural_light_zones_pct <= 100

    def test_generate_variants_count(self):
        """Test variant generation count."""
        calc = SpaceCalculator(200, 20, ["open-space", "meeting"])
        variants = calc.generate_variants(num_variants=3)

        assert len(variants) == 3
        assert variants[0].variant_id == "balanced-001"
        assert variants[1].variant_id == "collaboration-heavy-002"
        assert variants[2].variant_id == "focus-intensive-003"

    def test_variant_has_required_fields(self):
        """Test that each variant has required fields."""
        calc = SpaceCalculator(200, 20, ["open-space", "meeting"])
        variants = calc.generate_variants(num_variants=1)

        variant = variants[0]
        assert hasattr(variant, "variant_id")
        assert hasattr(variant, "layout_name")
        assert hasattr(variant, "zones")
        assert hasattr(variant, "metrics")
        assert hasattr(variant, "floorplan_stub_url")
        assert "stub:///" in variant.floorplan_stub_url

    def test_collaboration_style_affects_distribution(self):
        """Test that collaboration style changes zone distribution."""
        high_calc = SpaceCalculator(
            200, 20, ["open-space", "meeting", "break-room"],
            collaboration_style="high_collab"
        )
        low_calc = SpaceCalculator(
            200, 20, ["open-space", "meeting", "break-room"],
            collaboration_style="low_collab"
        )

        high_dist = high_calc._distribute_zones()
        low_dist = low_calc._distribute_zones()

        # High collab should have more meeting/break, low should have more open
        high_collab_zones = high_dist.get(ZoneType.MEETING, 0) + high_dist.get(ZoneType.BREAK_ROOM, 0)
        low_collab_zones = low_dist.get(ZoneType.MEETING, 0) + low_dist.get(ZoneType.BREAK_ROOM, 0)

        assert high_collab_zones >= low_collab_zones

    def test_generate_space_layouts_json_output(self):
        """Test JSON output format."""
        json_str = generate_space_layouts_json(
            surface_sqm=200,
            headcount=20,
            zone_types=["open-space", "meeting"],
            project_id="test-proj-1"
        )

        data = json.loads(json_str)
        assert data["project_id"] == "test-proj-1"
        assert "brief" in data
        assert "variants" in data
        assert len(data["variants"]) == 3

        # Check variant structure
        for variant in data["variants"]:
            assert "variant_id" in variant
            assert "zones" in variant
            assert "metrics" in variant
            assert "floorplan_stub_url" in variant
            assert variant["metrics"]["total_sqm"] == 200

    def test_edge_case_small_space(self):
        """Test with small workspace."""
        calc = SpaceCalculator(
            surface_sqm=50,
            headcount=5,
            zone_types=["open-space", "meeting"]
        )
        variants = calc.generate_variants(num_variants=1)

        assert len(variants) == 1
        assert variants[0].metrics.total_sqm == 50
        assert variants[0].metrics.average_sqm_per_person == 10

    def test_edge_case_large_space(self):
        """Test with large workspace."""
        calc = SpaceCalculator(
            surface_sqm=1000,
            headcount=100,
            zone_types=["open-space", "meeting", "quiet-zone", "phone-booth", "break-room"]
        )
        variants = calc.generate_variants(num_variants=1)

        assert len(variants) == 1
        assert variants[0].metrics.total_sqm == 1000
        assert variants[0].metrics.workstations > 0
        assert variants[0].metrics.meeting_rooms > 0

    def test_all_zone_types(self):
        """Test with all zone types."""
        all_zones = [
            "open-space", "meeting", "quiet-zone",
            "phone-booth", "break-room", "storage"
        ]
        calc = SpaceCalculator(500, 50, all_zones)
        variants = calc.generate_variants(num_variants=1)

        variant = variants[0]
        zone_types = {z.zone_type for z in variant.zones}

        # Should have at least open space
        assert len(variant.zones) > 0

    def test_consistency_across_calls(self):
        """Test that same input produces same output (deterministic)."""
        json1 = generate_space_layouts_json(200, 20, ["open-space", "meeting"])
        json2 = generate_space_layouts_json(200, 20, ["open-space", "meeting"])

        data1 = json.loads(json1)
        data2 = json.loads(json2)

        # Should produce identical layouts
        assert data1 == data2


class TestSpaceCalculatorValidation:
    """Test validation and error handling."""

    def test_negative_surface_handled_gracefully(self):
        """Test that negative surface doesn't crash (validation at tool level)."""
        # Calculator accepts any numeric value; validation happens in server.py
        calc = SpaceCalculator(-100, 20, [])
        # Should still create object; validation occurs at tool level
        assert calc.surface_sqm == -100

    def test_negative_headcount_handled_gracefully(self):
        """Test that negative headcount doesn't crash (validation at tool level)."""
        # Calculator accepts any numeric value; validation happens in server.py
        calc = SpaceCalculator(200, -20, [])
        # Should still create object; validation occurs at tool level
        assert calc.headcount == -20

    def test_empty_zone_types(self):
        """Test with empty zone types."""
        calc = SpaceCalculator(200, 20, [])
        # Should handle gracefully
        variants = calc.generate_variants(num_variants=1)
        # May produce storage only or empty layout


class TestZoneCreation:
    """Test zone object creation."""

    def test_meeting_room_creation(self):
        """Test meeting room zone creation."""
        calc = SpaceCalculator(200, 20, ["meeting"])
        distribution = {ZoneType.MEETING: 12}  # 12 people = 3 meeting rooms
        zones = calc._create_zones(distribution)

        meeting_zones = [z for z in zones if z.zone_type == "meeting"]
        assert len(meeting_zones) >= 1
        for zone in meeting_zones:
            assert zone.occupancy == 4  # Standard 4-person rooms

    def test_phone_booth_creation(self):
        """Test phone booth creation."""
        calc = SpaceCalculator(200, 20, ["phone-booth"])
        distribution = {ZoneType.PHONE_BOOTH: 3}  # 3 people needing phone booths
        zones = calc._create_zones(distribution)

        booth_zones = [z for z in zones if z.zone_type == "phone-booth"]
        # 3 people = 2 booths (1-2 people per booth to prevent thin sliver rectangles)
        assert len(booth_zones) == 2
        # First booth has 2 people, second booth has 1
        assert booth_zones[0].occupancy == 2
        assert booth_zones[1].occupancy == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
