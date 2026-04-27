"""Ecovacs sensor module."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from deebot_client.capabilities import (
    CapabilityEvent,
    CapabilityLifeSpan,
# BEGIN CUSTOM CODE
    CapabilityMap,
# END CUSTOM CODE
    DeviceType,
)
from deebot_client.device import Device
from deebot_client.events import (
    BatteryEvent,
    CachedMapInfoEvent,
    ErrorEvent,
    Event,
    LifeSpan,
    LifeSpanEvent,
    NetworkInfoEvent,
# BEGIN CUSTOM CODE
    PositionsEvent,
    RoomsEvent,
# END CUSTOM CODE
    StatsEvent,
    TotalStatsEvent,
    station,
)
from sucks import VacBot

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    ATTR_BATTERY_LEVEL,
    CONF_DESCRIPTION,
    PERCENTAGE,
    EntityCategory,
    UnitOfArea,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.icon import icon_for_battery_level
from homeassistant.helpers.typing import StateType

from . import EcovacsConfigEntry
from .const import LEGACY_SUPPORTED_LIFESPANS, SUPPORTED_LIFESPANS
from .entity import (
    EcovacsCapabilityEntityDescription,
    EcovacsDescriptionEntity,
    EcovacsEntity,
    EcovacsLegacyEntity,
)
from .util import get_name_key, get_options, get_supported_entities


@dataclass(kw_only=True, frozen=True)
class EcovacsSensorEntityDescription[EventT: Event](
    EcovacsCapabilityEntityDescription,
    SensorEntityDescription,
):
    """Ecovacs sensor entity description."""

    value_fn: Callable[[EventT], StateType]
    native_unit_of_measurement_fn: Callable[[DeviceType], str | None] | None = None


@callback
def get_area_native_unit_of_measurement(device_type: DeviceType) -> str | None:
    """Get the area native unit of measurement based on device type."""
    if device_type is DeviceType.MOWER:
        return UnitOfArea.SQUARE_CENTIMETERS
    return UnitOfArea.SQUARE_METERS


ENTITY_DESCRIPTIONS: tuple[EcovacsSensorEntityDescription, ...] = (
    # Stats
    EcovacsSensorEntityDescription[StatsEvent](
        key="stats_area",
        capability_fn=lambda caps: caps.stats.clean,
        value_fn=lambda e: e.area,
        translation_key="stats_area",
        device_class=SensorDeviceClass.AREA,
        native_unit_of_measurement_fn=get_area_native_unit_of_measurement,
        suggested_unit_of_measurement=UnitOfArea.SQUARE_METERS,
    ),
    EcovacsSensorEntityDescription[StatsEvent](
        key="stats_time",
        capability_fn=lambda caps: caps.stats.clean,
        value_fn=lambda e: e.time,
        translation_key="stats_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
    ),
    # TotalStats
    EcovacsSensorEntityDescription[TotalStatsEvent](
        capability_fn=lambda caps: caps.stats.total,
        value_fn=lambda e: e.area,
        key="total_stats_area",
        translation_key="total_stats_area",
        device_class=SensorDeviceClass.AREA,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    EcovacsSensorEntityDescription[TotalStatsEvent](
        capability_fn=lambda caps: caps.stats.total,
        value_fn=lambda e: e.time,
        key="total_stats_time",
        translation_key="total_stats_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    EcovacsSensorEntityDescription[TotalStatsEvent](
        capability_fn=lambda caps: caps.stats.total,
        value_fn=lambda e: e.cleanings,
        key="total_stats_cleanings",
        translation_key="total_stats_cleanings",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    EcovacsSensorEntityDescription[BatteryEvent](
        capability_fn=lambda caps: caps.battery,
        value_fn=lambda e: e.value,
        key=ATTR_BATTERY_LEVEL,
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcovacsSensorEntityDescription[NetworkInfoEvent](
        capability_fn=lambda caps: caps.network,
        value_fn=lambda e: e.ip,
        key="network_ip",
        translation_key="network_ip",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcovacsSensorEntityDescription[NetworkInfoEvent](
        capability_fn=lambda caps: caps.network,
        value_fn=lambda e: e.rssi,
        key="network_rssi",
        translation_key="network_rssi",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcovacsSensorEntityDescription[NetworkInfoEvent](
        capability_fn=lambda caps: caps.network,
        value_fn=lambda e: e.ssid,
        key="network_ssid",
        translation_key="network_ssid",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Station
    EcovacsSensorEntityDescription[station.StationEvent](
        capability_fn=lambda caps: caps.station.state if caps.station else None,
        value_fn=lambda e: get_name_key(e.state),
        key="station_state",
        translation_key="station_state",
        device_class=SensorDeviceClass.ENUM,
        options=get_options(station.State),
    ),
)


@dataclass(kw_only=True, frozen=True)
class EcovacsLifespanSensorEntityDescription(SensorEntityDescription):
    """Ecovacs lifespan sensor entity description."""

    component: LifeSpan
    value_fn: Callable[[LifeSpanEvent], int | float]


LIFESPAN_ENTITY_DESCRIPTIONS = tuple(
    EcovacsLifespanSensorEntityDescription(
        component=component,
        value_fn=lambda e: e.percent,
        key=f"lifespan_{component.name.lower()}",
        translation_key=f"lifespan_{component.name.lower()}",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
    )
    for component in SUPPORTED_LIFESPANS
)


@dataclass(kw_only=True, frozen=True)
class EcovacsLegacyLifespanSensorEntityDescription(SensorEntityDescription):
    """Ecovacs lifespan sensor entity description."""

    component: str


LEGACY_LIFESPAN_SENSORS = tuple(
    EcovacsLegacyLifespanSensorEntityDescription(
        component=component,
        key=f"lifespan_{component}",
        translation_key=f"lifespan_{component}",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
    )
    for component in LEGACY_SUPPORTED_LIFESPANS
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add entities for passed config_entry in HA."""
    controller = config_entry.runtime_data

    entities: list[EcovacsEntity] = get_supported_entities(
        controller, EcovacsSensor, ENTITY_DESCRIPTIONS
    )
    entities.extend(
        EcovacsLifespanSensor(device, device.capabilities.life_span, description)
        for device in controller.devices
        for description in LIFESPAN_ENTITY_DESCRIPTIONS
        if description.component in device.capabilities.life_span.types
    )
    entities.extend(
        EcovacsErrorSensor(device, capability)
        for device in controller.devices
        if (capability := device.capabilities.error)
    )
    # BEGIN CUSTOM CODE
    entities.extend(
        EcovacsCurrentRoomSensor(device, caps)
        for device in controller.devices
        if (caps := device.capabilities.map)
    )
    entities.extend(
        EcovacsStationCurrentRoomSensor(device, caps)
        for device in controller.devices
        if (caps := device.capabilities.map)
    )
    # END CUSTOM CODE

    async_add_entities(entities)

    async def _add_legacy_lifespan_entities() -> None:
        entities = []
        for device in controller.legacy_devices:
            for description in LEGACY_LIFESPAN_SENSORS:
                if (
                    description.component in device.components
                    and not controller.legacy_entity_is_added(
                        device, description.component
                    )
                ):
                    controller.add_legacy_entity(device, description.component)
                    entities.append(EcovacsLegacyLifespanSensor(device, description))

        if entities:
            async_add_entities(entities)

    def _fire_ecovacs_legacy_lifespan_event(_: Any) -> None:
        hass.create_task(_add_legacy_lifespan_entities())

    legacy_entities = []
    for device in controller.legacy_devices:
        config_entry.async_on_unload(
            device.lifespanEvents.subscribe(
                _fire_ecovacs_legacy_lifespan_event
            ).unsubscribe
        )
        if not controller.legacy_entity_is_added(device, "battery_status"):
            controller.add_legacy_entity(device, "battery_status")
            legacy_entities.append(EcovacsLegacyBatterySensor(device))

    if legacy_entities:
        async_add_entities(legacy_entities)


class EcovacsSensor(
    EcovacsDescriptionEntity[CapabilityEvent],
    SensorEntity,
):
    """Ecovacs sensor."""

    entity_description: EcovacsSensorEntityDescription

    def __init__(
        self,
        device: Device,
        capability: CapabilityEvent,
        entity_description: EcovacsSensorEntityDescription,
        **kwargs: Any,
    ) -> None:
        """Initialize entity."""
        super().__init__(device, capability, entity_description, **kwargs)
        if (
            entity_description.native_unit_of_measurement_fn
            and (
                native_unit_of_measurement
                := entity_description.native_unit_of_measurement_fn(
                    device.capabilities.device_type
                )
            )
            is not None
        ):
            self._attr_native_unit_of_measurement = native_unit_of_measurement

    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_event(event: Event) -> None:
            value = self.entity_description.value_fn(event)
            if value is None:
                return

            self._attr_native_value = value
            self.async_write_ha_state()

        self._subscribe(self._capability.event, on_event)


class EcovacsLifespanSensor(
    EcovacsDescriptionEntity[CapabilityLifeSpan],
    SensorEntity,
):
    """Lifespan sensor."""

    entity_description: EcovacsLifespanSensorEntityDescription

    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_event(event: LifeSpanEvent) -> None:
            if event.type == self.entity_description.component:
                self._attr_native_value = self.entity_description.value_fn(event)
                self.async_write_ha_state()

        self._subscribe(self._capability.event, on_event)


class EcovacsErrorSensor(
    EcovacsEntity[CapabilityEvent[ErrorEvent]],
    SensorEntity,
):
    """Error sensor."""

    _always_available = True
    _unrecorded_attributes = frozenset({CONF_DESCRIPTION})
    entity_description: SensorEntityDescription = SensorEntityDescription(
        key="error",
        translation_key="error",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    )

    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_event(event: ErrorEvent) -> None:
            self._attr_native_value = event.code
            self._attr_extra_state_attributes = {CONF_DESCRIPTION: event.description}

            self.async_write_ha_state()

        self._subscribe(self._capability.event, on_event)


class EcovacsLegacyBatterySensor(EcovacsLegacyEntity, SensorEntity):
    """Legacy battery sensor."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        device: VacBot,
    ) -> None:
        """Initialize the entity."""
        super().__init__(device)
        self._attr_unique_id = f"{device.vacuum['did']}_battery_status"

    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        self._event_listeners.append(
            self.device.batteryEvents.subscribe(
                lambda _: self.schedule_update_ha_state()
            )
        )

    @property
    def native_value(self) -> StateType:
        """Return the value reported by the sensor."""
        if (status := self.device.battery_status) is not None:
            return status * 100  # type: ignore[no-any-return]
        return None

    @property
    def icon(self) -> str | None:
        """Return the icon to use in the frontend, if any."""
        return icon_for_battery_level(
            battery_level=self.native_value, charging=self.device.is_charging
        )


class EcovacsLegacyLifespanSensor(EcovacsLegacyEntity, SensorEntity):
    """Legacy Lifespan sensor."""

    entity_description: EcovacsLegacyLifespanSensorEntityDescription

    def __init__(
        self,
        device: VacBot,
        description: EcovacsLegacyLifespanSensorEntityDescription,
    ) -> None:
        """Initialize the entity."""
        super().__init__(device)
        self.entity_description = description
        self._attr_unique_id = f"{device.vacuum['did']}_{description.key}"

        if (value := device.components.get(description.component)) is not None:
            value = int(value * 100)
        self._attr_native_value = value

    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""

        def on_event(_: Any) -> None:
            if (
                value := self.device.components.get(self.entity_description.component)
            ) is not None:
                value = int(value * 100)
            self._attr_native_value = value
            self.schedule_update_ha_state()

        self._event_listeners.append(self.device.lifespanEvents.subscribe(on_event))


_SENSOR_LOGGER = logging.getLogger(__name__)


# BEGIN CUSTOM CODE
def _parse_coordinates(coords_str: str) -> list[tuple[int, int]]:
    """Parse semicolon-delimited 'x,y' coordinate string into a list of tuples."""
    vertices = []
    for pair in coords_str.split(";"):
        parts = pair.split(",")
        if len(parts) == 2:
            try:
                vertices.append((int(parts[0]), int(parts[1])))
            except ValueError:
                pass
    return vertices


def _point_in_polygon(px: int, py: int, vertices: list[tuple[int, int]]) -> bool:
    """Return True if point (px, py) is inside the polygon using ray casting."""
    inside = False
    n = len(vertices)
    j = n - 1
    for i in range(n):
        xi, yi = vertices[i]
        xj, yj = vertices[j]
        if ((yi > py) != (yj > py)) and (
            px < (xj - xi) * (py - yi) / (yj - yi) + xi
        ):
            inside = not inside
        j = i
    return inside


def _resolve_room_for_coordinates(
    rooms: list[Any],
    x: int,
    y: int,
) -> tuple[str | None, int | None]:
    """Resolve the room name/id for given map coordinates."""
    for room in rooms:
        vertices = _parse_coordinates(room.coordinates)
        if vertices and _point_in_polygon(x, y, vertices):
            return room.name, room.id

    return None, None


_ROOMS_REFRESH_REQUESTED_DIDS: set[str] = set()
_ROOM_CENTERS_FETCHED_DIDS: set[str] = set()


def _extract_room_centers_from_subsets(subsets_b64: str) -> list[dict[str, Any]]:
    """Decode zstd-compressed subsets payload and return list of room center dicts."""
    try:
        payload = base64.b64decode(subsets_b64, validate=True)
    except (binascii.Error, ValueError):
        return []

    decompressed: bytes | None = None

    if payload.startswith(b"\x28\xB5\x2F\xFD"):
        try:
            import zstandard  # type: ignore[import-not-found]

            decompressed = zstandard.ZstdDecompressor().decompress(payload)
        except Exception:  # noqa: BLE001
            return []
    else:
        import gzip
        import lzma
        import zlib

        for decoder in [
            lambda d: zlib.decompress(d),
            lambda d: gzip.decompress(d),
            lambda d: lzma.decompress(d),
            lambda d: zlib.decompress(d, -zlib.MAX_WBITS),
        ]:
            try:
                decompressed = decoder(payload)
                break
            except Exception:  # noqa: BLE001
                pass

    if decompressed is None:
        return []

    try:
        parsed = json.loads(decompressed.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []

    if not isinstance(parsed, list):
        return []

    centers: list[dict[str, Any]] = []
    for entry in parsed:
        if not isinstance(entry, list) or len(entry) < 7:
            continue
        try:
            centers.append({
                "id": int(entry[0]),
                "name": str(entry[1]).strip() or f"Room {entry[0]}",
                "x": int(entry[5]),
                "y": int(entry[6]),
            })
        except (ValueError, TypeError, IndexError):
            pass

    return centers


def _resolve_room_by_nearest_center(
    centers: list[dict[str, Any]],
    x: int,
    y: int,
) -> tuple[str | None, int | None]:
    """Return (name, id) of the room whose center is nearest to (x, y)."""
    if not centers:
        return None, None
    best = min(centers, key=lambda c: (c["x"] - x) ** 2 + (c["y"] - y) ** 2)
    return best["name"], best["id"]


async def _fetch_room_centers_once(
    device: Device,
    capability: CapabilityMap,
) -> list[dict[str, Any]]:
    """Fetch and decode room centers from GetMapSetV2 (once per device)."""
    did = device.device_info["did"]
    if did in _ROOM_CENTERS_FETCHED_DIDS:
        return []
    _ROOM_CENTERS_FETCHED_DIDS.add(did)

    if capability.set is None:
        return []

    # Resolve active map id
    map_id: str | None = None
    if cached := device.events.get_last_event(CachedMapInfoEvent):
        map_id = next(
            (m.id for m in cached.maps if m.using), None
        )

    if map_id is None and capability.rooms.get:
        try:
            async with asyncio.timeout(20):
                raw = await device.execute_command(capability.rooms.get[0])
        except Exception:  # noqa: BLE001
            return []
        for m in raw.get("resp", {}).get("body", {}).get("data", {}).get("info", []):
            if m.get("using") == 1 and m.get("mid") not in (None, "0", ""):
                map_id = m["mid"]
                break

    if map_id is None:
        return []

    try:
        async with asyncio.timeout(20):
            resp = await device.execute_command(capability.set.execute(map_id))
    except Exception:  # noqa: BLE001
        return []

    subsets = resp.get("resp", {}).get("body", {}).get("data", {}).get("subsets")
    if not isinstance(subsets, str) or not subsets:
        return []

    return _extract_room_centers_from_subsets(subsets)


def _request_rooms_refresh_once(device: Device, capability: CapabilityMap) -> None:
    """Request a one-time rooms refresh for devices that don't push rooms eagerly."""
    did = device.device_info["did"]
    if did in _ROOMS_REFRESH_REQUESTED_DIDS:
        return

    _ROOMS_REFRESH_REQUESTED_DIDS.add(did)
    device.events.request_refresh(capability.rooms.event)


class EcovacsCurrentRoomSensor(
    EcovacsEntity[CapabilityMap],
    SensorEntity,
):
    """Sensor reporting the room the vacuum is currently in."""

    entity_description = SensorEntityDescription(
        key="current_room",
        translation_key="current_room",
    )

    def __init__(self, device: Device, capability: CapabilityMap) -> None:
        """Initialize entity."""
        super().__init__(device, capability)
        # Force a fresh registry entry so HA doesn't reuse an older incorrect entity_id.
        self._attr_unique_id = (
            f"{device.device_info['did']}_{self.entity_description.key}_sensor"
        )
        self._attr_name = "Current room"
        self._rooms: list = []
        self._room_centers: list[dict[str, Any]] = []
        self._map_id: str | None = None
        self._attr_native_value: str | None = None
        self._attr_extra_state_attributes = {
            "x": None,
            "y": None,
            "angle": None,
            "room_id": None,
            "map_id": None,
            "available_room_ids": [],
            "available_rooms": {},
        }

    def _set_room_from_position(self, x: int, y: int, angle: int | None) -> None:
        """Update state from the provided map position."""
        self._attr_extra_state_attributes["x"] = x
        self._attr_extra_state_attributes["y"] = y
        self._attr_extra_state_attributes["angle"] = angle
        self._attr_extra_state_attributes["map_id"] = self._map_id

        room_name, room_id = _resolve_room_for_coordinates(self._rooms, x, y)
        if room_name is None and self._room_centers:
            room_name, room_id = _resolve_room_by_nearest_center(
                self._room_centers, x, y
            )
        self._attr_native_value = room_name
        self._attr_extra_state_attributes["room_id"] = room_id
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_rooms(event: RoomsEvent) -> None:
            self._rooms = event.rooms
            self._map_id = event.map_id
            self._attr_extra_state_attributes["map_id"] = event.map_id
            self._attr_extra_state_attributes["available_room_ids"] = [
                room.id for room in event.rooms
            ]
            self._attr_extra_state_attributes["available_rooms"] = {
                room.name: room.id for room in event.rooms
            }

        async def on_positions(event: PositionsEvent) -> None:
            if not self._rooms:
                _request_rooms_refresh_once(self._device, self._capability)
                if not self._room_centers:
                    centers = await _fetch_room_centers_once(
                        self._device, self._capability
                    )
                    if centers:
                        self._room_centers = centers
                        self._attr_extra_state_attributes["available_room_ids"] = [
                            c["id"] for c in centers
                        ]
                        self._attr_extra_state_attributes["available_rooms"] = {
                            c["name"]: c["id"] for c in centers
                        }

            for pos in event.positions:
                position_type = getattr(pos.type, "name", str(pos.type)).upper()
                if "DEEBOT" not in position_type:
                    continue

                self._set_room_from_position(pos.x, pos.y, pos.a)
                return

        self._subscribe(self._capability.rooms.event, on_rooms)
        self._subscribe(self._capability.position.event, on_positions)

        if last_rooms := self._device.events.get_last_event(RoomsEvent):
            await on_rooms(last_rooms)
        if last_positions := self._device.events.get_last_event(PositionsEvent):
            await on_positions(last_positions)


class EcovacsStationCurrentRoomSensor(
    EcovacsEntity[CapabilityMap],
    SensorEntity,
):
    """Sensor reporting the room of the base station."""

    entity_description = SensorEntityDescription(
        key="station_current_room",
        translation_key="station_current_room",
        name="Station current room",
    )

    def __init__(self, device: Device, capability: CapabilityMap) -> None:
        """Initialize entity."""
        super().__init__(device, capability)
        # Force a fresh registry entry so HA doesn't reuse an older incorrect entity_id.
        self._attr_unique_id = (
            f"{device.device_info['did']}_{self.entity_description.key}_sensor"
        )
        self._attr_name = "Station current room"
        self._rooms: list = []
        self._room_centers: list[dict[str, Any]] = []
        self._map_id: str | None = None
        self._attr_native_value: str | None = None
        self._attr_extra_state_attributes = {
            "x": None,
            "y": None,
            "angle": None,
            "room_id": None,
            "map_id": None,
            "available_room_ids": [],
            "available_rooms": {},
        }

    def _set_base_station_from_position(self, x: int, y: int, angle: int | None) -> None:
        """Update state from the provided base station map position."""
        self._attr_extra_state_attributes["x"] = x
        self._attr_extra_state_attributes["y"] = y
        self._attr_extra_state_attributes["angle"] = angle
        self._attr_extra_state_attributes["map_id"] = self._map_id

        room_name, room_id = _resolve_room_for_coordinates(self._rooms, x, y)
        if room_name is None and self._room_centers:
            room_name, room_id = _resolve_room_by_nearest_center(
                self._room_centers, x, y
            )
        self._attr_native_value = room_name
        self._attr_extra_state_attributes["room_id"] = room_id
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_rooms(event: RoomsEvent) -> None:
            self._rooms = event.rooms
            self._map_id = event.map_id
            self._attr_extra_state_attributes["map_id"] = event.map_id
            self._attr_extra_state_attributes["available_room_ids"] = [
                room.id for room in event.rooms
            ]
            self._attr_extra_state_attributes["available_rooms"] = {
                room.name: room.id for room in event.rooms
            }

        async def on_positions(event: PositionsEvent) -> None:
            if not self._rooms:
                _request_rooms_refresh_once(self._device, self._capability)
                if not self._room_centers:
                    centers = await _fetch_room_centers_once(
                        self._device, self._capability
                    )
                    if centers:
                        self._room_centers = centers
                        self._attr_extra_state_attributes["available_room_ids"] = [
                            c["id"] for c in centers
                        ]
                        self._attr_extra_state_attributes["available_rooms"] = {
                            c["name"]: c["id"] for c in centers
                        }

            for pos in event.positions:
                position_type = getattr(pos.type, "name", str(pos.type)).upper()
                if any(token in position_type for token in ("CHARG", "STATION", "DOCK")):
                    self._set_base_station_from_position(pos.x, pos.y, pos.a)
                    return

        self._subscribe(self._capability.rooms.event, on_rooms)
        self._subscribe(self._capability.position.event, on_positions)

        if last_rooms := self._device.events.get_last_event(RoomsEvent):
            await on_rooms(last_rooms)
        if last_positions := self._device.events.get_last_event(PositionsEvent):
            await on_positions(last_positions)
# END CUSTOM CODE
