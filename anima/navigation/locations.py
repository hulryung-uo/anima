"""World knowledge — static city, moongate, and resource data loaded from YAML.

This is 'common knowledge' any character would have.
Specific discovered locations are stored in the memory DB.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(slots=True)
class CityInfo:
    """Information about a known city."""

    key: str  # e.g. "britain"
    name: str  # e.g. "Britain"
    description: str
    center_x: int
    center_y: int
    bounds: tuple[int, int, int, int]  # x1, y1, x2, y2
    known_features: list[str] = field(default_factory=list)
    resources_nearby: list[str] = field(default_factory=list)
    warning: str | None = None


@dataclass(slots=True)
class MoongateInfo:
    """A public moongate location."""

    name: str
    x: int
    y: int


@dataclass(slots=True)
class ResourceHint:
    """A general area known for a particular resource."""

    resource_type: str  # "lumber", "mining", "fishing"
    description: str
    area: tuple[int, int, int, int]  # x1, y1, x2, y2


def _distance(x1: int, y1: int, x2: int, y2: int) -> float:
    """Euclidean distance between two points."""
    dx = x1 - x2
    dy = y1 - y2
    return math.sqrt(dx * dx + dy * dy)


def _area_center(area: tuple[int, int, int, int]) -> tuple[int, int]:
    """Center of a rectangular area."""
    return (area[0] + area[2]) // 2, (area[1] + area[3]) // 2


class WorldKnowledge:
    """Static world knowledge loaded from YAML.

    This is 'common knowledge' any character would have.
    Specific discovered locations are stored in the memory DB.
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        if data_dir is None:
            data_dir = Path(__file__).parent.parent.parent / "data"
        self._data_dir = data_dir

        self._cities: dict[str, CityInfo] = {}
        self._moongates: list[MoongateInfo] = []
        self._resource_hints: dict[str, list[ResourceHint]] = {}

        self._load()

    def _load(self) -> None:
        path = self._data_dir / "world_knowledge.yaml"
        if not path.exists():
            return

        with open(path) as f:
            raw = yaml.safe_load(f) or {}

        # Cities
        for key, city_data in (raw.get("cities") or {}).items():
            center = city_data.get("center", [0, 0])
            bounds = city_data.get("bounds", [0, 0, 0, 0])
            self._cities[key] = CityInfo(
                key=key,
                name=city_data.get("name", key),
                description=city_data.get("description", ""),
                center_x=center[0],
                center_y=center[1],
                bounds=(bounds[0], bounds[1], bounds[2], bounds[3]),
                known_features=city_data.get("known_features", []),
                resources_nearby=city_data.get("resources_nearby", []),
                warning=city_data.get("warning"),
            )

        # Moongates
        for gate_data in raw.get("moongates") or []:
            loc = gate_data.get("location", [0, 0])
            self._moongates.append(
                MoongateInfo(
                    name=gate_data.get("name", ""),
                    x=loc[0],
                    y=loc[1],
                )
            )

        # Resource hints
        for resource_type, hints in (raw.get("resource_hints") or {}).items():
            self._resource_hints[resource_type] = []
            for hint_data in hints:
                area = hint_data.get("area", [0, 0, 0, 0])
                self._resource_hints[resource_type].append(
                    ResourceHint(
                        resource_type=resource_type,
                        description=hint_data.get("description", ""),
                        area=(area[0], area[1], area[2], area[3]),
                    )
                )

    @property
    def cities(self) -> dict[str, CityInfo]:
        """All known cities keyed by slug."""
        return self._cities

    @property
    def moongates(self) -> list[MoongateInfo]:
        """All known public moongates."""
        return list(self._moongates)

    def nearest_city(self, x: int, y: int) -> CityInfo | None:
        """Find the nearest city to the given coordinates."""
        if not self._cities:
            return None
        return min(
            self._cities.values(),
            key=lambda c: _distance(x, y, c.center_x, c.center_y),
        )

    def city_by_name(self, name: str) -> CityInfo | None:
        """Find a city by name (case-insensitive partial match)."""
        name_lower = name.lower()
        # Exact key match first
        if name_lower in self._cities:
            return self._cities[name_lower]
        # Partial name match
        for city in self._cities.values():
            if name_lower in city.name.lower():
                return city
        # Partial key match
        for key, city in self._cities.items():
            if name_lower in key:
                return city
        return None

    def city_with_feature(self, feature: str) -> list[CityInfo]:
        """Find cities that have a specific feature (e.g., 'carpenter')."""
        feature_lower = feature.lower()
        return [
            city
            for city in self._cities.values()
            if any(feature_lower in f.lower() for f in city.known_features)
        ]

    def current_city(self, x: int, y: int) -> CityInfo | None:
        """Check if coordinates are inside a city's bounds."""
        for city in self._cities.values():
            x1, y1, x2, y2 = city.bounds
            if x1 <= x <= x2 and y1 <= y <= y2:
                return city
        return None

    def nearest_moongate(self, x: int, y: int) -> MoongateInfo | None:
        """Find the nearest moongate."""
        if not self._moongates:
            return None
        return min(
            self._moongates,
            key=lambda g: _distance(x, y, g.x, g.y),
        )

    def resource_areas(self, resource_type: str) -> list[ResourceHint]:
        """Get known areas for a resource type."""
        return list(self._resource_hints.get(resource_type, []))

    def nearest_resource(self, x: int, y: int, resource_type: str) -> ResourceHint | None:
        """Find the nearest known resource area."""
        hints = self._resource_hints.get(resource_type)
        if not hints:
            return None
        return min(
            hints,
            key=lambda h: _distance(x, y, *_area_center(h.area)),
        )
