"""Support for Ecovacs Ecovacs Vacuums."""
from __future__ import annotations

import asyncio
import base64
import binascii
import bz2
from collections.abc import Mapping
import gzip
import json
import lzma
import logging
from typing import TYPE_CHECKING, Any
import zlib

from deebot_client.capabilities import Capabilities, DeviceType
from deebot_client.device import Device
from deebot_client.events import (
    CachedMapInfoEvent,
    FanSpeedEvent,
    RoomsEvent,
    StateEvent,
)
from deebot_client.events.map import Map
from deebot_client.models import CleanAction, CleanMode, State
import sucks

from homeassistant.components.vacuum import (
    Segment,
    StateVacuumEntity,
    StateVacuumEntityDescription,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import slugify

from . import EcovacsConfigEntry
from .const import DOMAIN
from .entity import EcovacsEntity, EcovacsLegacyEntity
from .util import get_name_key

_LOGGER = logging.getLogger(__name__)
_SEGMENTS_SEPARATOR = "_"

ATTR_ERROR = "error"


# BEGIN CUSTOM CODE
def _decode_map_subsets_payload(subsets: str) -> dict[str, Any]:
    """Decode and inspect map subsets payload."""
    result: dict[str, Any] = {
        "subsets_length": len(subsets),
        "base64_valid": False,
    }

    try:
        payload = base64.b64decode(subsets, validate=True)
    except (binascii.Error, ValueError) as err:
        result["error"] = f"invalid_base64: {err}"
        return result

    result["base64_valid"] = True
    result["decoded_length"] = len(payload)
    result["magic_hex"] = payload[:8].hex()
    result["magic_ascii"] = "".join(
        chr(byte) if 32 <= byte <= 126 else "." for byte in payload[:8]
    )
    result["decoded_preview_hex"] = payload[:64].hex()

    decompressed: bytes | None = None

    if payload.startswith(b"\x28\xB5\x2F\xFD"):
        result["zstd_magic_detected"] = True
        try:
            import zstd  # type: ignore[import-not-found]

            decompressed = zstd.decompress(payload)
            result["decompressor"] = "zstd"
        except Exception as err:  # pragma: no cover - optional dependency path
            result["zstd_decode_error"] = str(err)
            result["hint"] = (
                "zstd payload detected; install python package 'zstd' to decode"
            )
            return result
    else:
        candidates: list[tuple[str, Any]] = [
            ("zlib", zlib.decompress),
            ("gzip", gzip.decompress),
            ("bz2", bz2.decompress),
            ("lzma", lzma.decompress),
            ("deflate_raw", lambda data: zlib.decompress(data, -zlib.MAX_WBITS)),
        ]

        decode_errors: dict[str, str] = {}
        for name, decoder in candidates:
            try:
                decompressed = decoder(payload)
                result["decompressor"] = name
                break
            except Exception as err:  # pragma: no cover - diagnostic path
                decode_errors[name] = str(err)

        if decompressed is None:
            result["decode_errors"] = decode_errors
            return result

    result["decompressed_length"] = len(decompressed)
    result["decompressed_preview_hex"] = decompressed[:64].hex()

    try:
        decoded_text = decompressed.decode("utf-8")
        result["utf8_preview"] = decoded_text[:400]
        try:
            parsed = json.loads(decoded_text)
            result["json_parsed"] = parsed
            if isinstance(parsed, dict):
                result["json_keys"] = sorted(parsed.keys())
                if "rooms" in parsed:
                    result["rooms"] = parsed["rooms"]
            elif isinstance(parsed, list):
                result["rooms"] = parsed
        except json.JSONDecodeError as err:
            result["json_decode_error"] = str(err)
    except UnicodeDecodeError as err:
        result["utf8_decode_error"] = str(err)

    return result


def _extract_rooms_from_decoded_payload(decoded: Any) -> list[dict[str, Any]]:
    """Extract room id/name pairs from decoded map_set payload."""
    if not isinstance(decoded, list):
        return []

    rooms: list[dict[str, Any]] = []
    for entry in decoded:
        if not isinstance(entry, list) or len(entry) < 2:
            continue

        try:
            room_id = int(entry[0])
        except (TypeError, ValueError):
            continue

        room_name = str(entry[1]).strip() or f"Room {room_id}"
        rooms.append({
            "id": room_id,
            "name": room_name,
            "command_id": entry[0],
        })

    return rooms
# END CUSTOM CODE


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Ecovacs vacuums."""

    controller = config_entry.runtime_data
    vacuums: list[EcovacsVacuum | EcovacsLegacyVacuum] = [
        EcovacsVacuum(device)
        for device in controller.devices
        if device.capabilities.device_type is DeviceType.VACUUM
    ]
    vacuums.extend(
        [EcovacsLegacyVacuum(device) for device in controller.legacy_devices]
    )
    _LOGGER.debug("Adding Ecovacs Vacuums to Home Assistant: %s", vacuums)
    async_add_entities(vacuums)


class EcovacsLegacyVacuum(EcovacsLegacyEntity, StateVacuumEntity):
    """Legacy Ecovacs vacuums."""

    _attr_fan_speed_list = [sucks.FAN_SPEED_NORMAL, sucks.FAN_SPEED_HIGH]
    _attr_supported_features = (
        VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.CLEAN_SPOT
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.START
        | VacuumEntityFeature.LOCATE
        | VacuumEntityFeature.STATE
        | VacuumEntityFeature.SEND_COMMAND
        | VacuumEntityFeature.FAN_SPEED
    )

    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        self._event_listeners.append(
            self.device.statusEvents.subscribe(
                lambda _: self.schedule_update_ha_state()
            )
        )
        self._event_listeners.append(
            self.device.lifespanEvents.subscribe(
                lambda _: self.schedule_update_ha_state()
            )
        )
        self._event_listeners.append(self.device.errorEvents.subscribe(self.on_error))

    def on_error(self, error: str) -> None:
        """Handle an error event from the robot.

        This will not change the entity's state. If the error caused the state
        to change, that will come through as a separate on_status event
        """
        if error in ["no_error", sucks.ERROR_CODES["100"]]:
            self.error = None
        else:
            self.error = error

        self.hass.bus.fire(
            "ecovacs_error", {"entity_id": self.entity_id, "error": error}
        )
        self.schedule_update_ha_state()

    @property
    def activity(self) -> VacuumActivity | None:
        """Return the state of the vacuum cleaner."""
        if self.error is not None:
            return VacuumActivity.ERROR

        if self.device.is_cleaning:
            return VacuumActivity.CLEANING

        if self.device.is_charging:
            return VacuumActivity.DOCKED

        if self.device.vacuum_status == sucks.CLEAN_MODE_STOP:
            return VacuumActivity.IDLE

        if self.device.vacuum_status == sucks.CHARGE_MODE_RETURNING:
            return VacuumActivity.RETURNING

        return None

    @property
    def fan_speed(self) -> str | None:
        """Return the fan speed of the vacuum cleaner."""
        return self.device.fan_speed  # type: ignore[no-any-return]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the device-specific state attributes of this vacuum."""
        data: dict[str, Any] = {}
        data[ATTR_ERROR] = self.error

        return data

    def return_to_base(self, **kwargs: Any) -> None:
        """Set the vacuum cleaner to return to the dock."""

        self.device.run(sucks.Charge())

    def start(self, **kwargs: Any) -> None:
        """Turn the vacuum on and start cleaning."""

        self.device.run(sucks.Clean())

    def stop(self, **kwargs: Any) -> None:
        """Stop the vacuum cleaner."""

        self.device.run(sucks.Stop())

    def clean_spot(self, **kwargs: Any) -> None:
        """Perform a spot clean-up."""

        self.device.run(sucks.Spot())

    def locate(self, **kwargs: Any) -> None:
        """Locate the vacuum cleaner."""

        self.device.run(sucks.PlaySound())

    def set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        """Set fan speed."""
        if self.state == VacuumActivity.CLEANING:
            self.device.run(sucks.Clean(mode=self.device.clean_status, speed=fan_speed))

    def send_command(
        self,
        command: str,
        params: dict[str, Any] | list[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Send a command to a vacuum cleaner."""
        self.device.run(sucks.VacBotCommand(command, params))

    async def async_raw_get_positions(
        self,
    ) -> None:
        """Get bot and chargers positions."""
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="vacuum_raw_get_positions_not_supported",
        )


_STATE_TO_VACUUM_STATE = {
    State.IDLE: VacuumActivity.IDLE,
    State.CLEANING: VacuumActivity.CLEANING,
    State.RETURNING: VacuumActivity.RETURNING,
    State.DOCKED: VacuumActivity.DOCKED,
    State.ERROR: VacuumActivity.ERROR,
    State.PAUSED: VacuumActivity.PAUSED,
}

_ATTR_ROOMS = "rooms"


class EcovacsVacuum(
    EcovacsEntity[Capabilities],
    StateVacuumEntity,
):
    """Ecovacs vacuum."""

    _unrecorded_attributes = frozenset({_ATTR_ROOMS})

    _attr_supported_features = (
        VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.SEND_COMMAND
        | VacuumEntityFeature.LOCATE
        | VacuumEntityFeature.STATE
        | VacuumEntityFeature.START
    )

    entity_description = StateVacuumEntityDescription(
        key="vacuum", translation_key="vacuum", name=None
    )

    def __init__(self, device: Device) -> None:
        """Initialize the vacuum."""
        super().__init__(device, device.capabilities)

        self._quick_commands_lock = asyncio.Lock()
        self._quick_commands: list[dict[str, Any]] = []
        self._room_event: RoomsEvent | None = None
        self._fallback_rooms: list[dict[str, Any]] = []
        self._fallback_map_id: str | None = None
        self._maps: dict[str, Map] = {}

        if fan_speed := self._capability.fan_speed:
            self._attr_supported_features |= VacuumEntityFeature.FAN_SPEED
            self._attr_fan_speed_list = [
                get_name_key(level) for level in fan_speed.types
            ]

        if self._capability.map and self._capability.clean.action.area:
            self._attr_supported_features |= VacuumEntityFeature.CLEAN_AREA

    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_status(event: StateEvent) -> None:
            self._attr_activity = _STATE_TO_VACUUM_STATE[event.state]
            self.async_write_ha_state()

        self._subscribe(self._capability.state.event, on_status)

        if self._capability.fan_speed:

            async def on_fan_speed(event: FanSpeedEvent) -> None:
                self._attr_fan_speed = get_name_key(event.speed)
                self.async_write_ha_state()

            self._subscribe(self._capability.fan_speed.event, on_fan_speed)

        if map_caps := self._capability.map:

            async def on_rooms(event: RoomsEvent) -> None:
                self._room_event = event
                self._fallback_rooms = []
                self._fallback_map_id = None
                self._check_segments_changed()
                self.async_write_ha_state()

            self._subscribe(map_caps.rooms.event, on_rooms)

            async def on_map_info(event: CachedMapInfoEvent) -> None:
                self._maps = {map_obj.id: map_obj for map_obj in event.maps}
                fallback_loaded = await self._async_ensure_fallback_rooms()
                self._check_segments_changed()
                if fallback_loaded:
                    self.async_write_ha_state()

            self._subscribe(map_caps.cached_info.event, on_map_info)

            if last_rooms := self._device.events.get_last_event(RoomsEvent):
                await on_rooms(last_rooms)
            if last_map_info := self._device.events.get_last_event(CachedMapInfoEvent):
                await on_map_info(last_map_info)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return entity specific state attributes.

        Implemented by platform classes. Convention for attribute names
        is lowercase snake_case.
        """
        rooms: dict[str, Any] = {}
        room_entries: list[tuple[str, int]] = []

        if self._room_event is not None:
            room_entries = [(room.name, room.id) for room in self._room_event.rooms]
        elif self._fallback_rooms:
            room_entries = [
                (str(room["name"]), int(room["id"]))
                for room in self._fallback_rooms
                if "name" in room and "id" in room
            ]

        if not room_entries:
            return rooms

        for room_name_raw, room_id in room_entries:
            # convert room name to snake_case to meet the convention
            room_name = slugify(room_name_raw)
            room_values = rooms.get(room_name)
            if room_values is None:
                rooms[room_name] = room_id
            elif isinstance(room_values, list):
                room_values.append(room_id)
            else:
                # Convert from int to list
                rooms[room_name] = [room_values, room_id]

        return {
            _ATTR_ROOMS: rooms,
        }

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        """Set fan speed."""
        if TYPE_CHECKING:
            assert self._capability.fan_speed
        await self._device.execute_command(self._capability.fan_speed.set(fan_speed))

    async def async_return_to_base(self, **kwargs: Any) -> None:
        """Set the vacuum cleaner to return to the dock."""
        await self._device.execute_command(self._capability.charge.execute())

    async def async_stop(self, **kwargs: Any) -> None:
        """Stop the vacuum cleaner."""
        await self._clean_command(CleanAction.STOP)

    async def async_pause(self) -> None:
        """Pause the vacuum cleaner."""
        await self._clean_command(CleanAction.PAUSE)

    async def async_start(self) -> None:
        """Start the vacuum cleaner."""
        await self._clean_command(CleanAction.START)

    async def _clean_command(self, action: CleanAction) -> None:
        await self._device.execute_command(
            self._capability.clean.action.command(action)
        )

    async def async_locate(self, **kwargs: Any) -> None:
        """Locate the vacuum cleaner."""
        await self._device.execute_command(self._capability.play_sound.execute())

    async def async_send_command(
        self,
        command: str,
        params: dict[str, Any] | list[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Send a command to a vacuum cleaner."""
        _LOGGER.debug("async_send_command %s with %s", command, params)
        if params is None:
            params = {}
        elif isinstance(params, list):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="vacuum_send_command_params_dict",
            )

        if command in ["spot_area", "custom_area"]:
            if params is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="vacuum_send_command_params_required",
                    translation_placeholders={"command": command},
                )
            if self._capability.clean.action.area is None:
                info = self._device.device_info
                name = info.get("nick", info["name"])
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="vacuum_send_command_area_not_supported",
                    translation_placeholders={"name": name},
                )

            if command == "spot_area":
                await self._device.execute_command(
                    self._capability.clean.action.area(
                        CleanMode.SPOT_AREA,
                        params["rooms"],
                        params.get("cleanings", 1),
                    )
                )
            elif command == "custom_area":
                await self._device.execute_command(
                    self._capability.clean.action.area(
                        CleanMode.CUSTOM_AREA,
                        params["coordinates"],
                        params.get("cleanings", 1),
                    )
                )
        else:
            await self._device.execute_command(
                self._capability.custom.set(command, params)
            )

    async def async_raw_get_positions(
        self,
    ) -> dict[str, Any]:
        """Get bot and chargers positions."""
        _LOGGER.debug("async_raw_get_positions")

        if not (map_cap := self._capability.map) or not (
            position_commands := map_cap.position.get
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="vacuum_raw_get_positions_not_supported",
            )

        return await self._device.execute_command(position_commands[0])

    # BEGIN CUSTOM CODE
    async def async_raw_get_map_info(
        self,
    ) -> dict[str, Any]:
        """Get raw cached map metadata."""
        _LOGGER.debug("async_raw_get_map_info")

        if not (map_cap := self._capability.map) or not (
            room_commands := map_cap.rooms.get
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="vacuum_raw_get_map_info_not_supported",
            )

        return await self._device.execute_command(room_commands[0])

    async def async_raw_get_map_set(
        self,
    ) -> dict[str, Any]:
        """Get raw map set metadata."""
        _LOGGER.debug("async_raw_get_map_set")

        if not (map_cap := self._capability.map) or map_cap.set is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="vacuum_raw_get_map_set_not_supported",
            )

        map_id = next(
            (map_obj.id for map_obj in self._maps.values() if map_obj.using),
            None,
        )

        if map_id is None and (
            cached_map_info := self._device.events.get_last_event(CachedMapInfoEvent)
        ):
            map_id = next(
                (map_obj.id for map_obj in cached_map_info.maps if map_obj.using),
                None,
            )

        if map_id is None and map_cap.rooms.get:
            try:
                async with asyncio.timeout(20):
                    raw_map_info = await self._device.execute_command(
                        map_cap.rooms.get[0]
                    )
            except TimeoutError as err:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="vacuum_raw_get_map_info_timeout",
                ) from err
            info = raw_map_info.get("resp", {}).get("body", {}).get("data", {}).get(
                "info", []
            )
            map_id = next(
                (
                    map_info.get("mid")
                    for map_info in info
                    if map_info.get("using") == 1 and map_info.get("mid") not in (None, "0", "")
                ),
                None,
            )

        if map_id is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="vacuum_raw_get_map_set_map_unavailable",
            )

        try:
            async with asyncio.timeout(20):
                return await self._device.execute_command(map_cap.set.execute(map_id))
        except TimeoutError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="vacuum_raw_get_map_set_timeout",
            ) from err

    async def async_raw_get_map_set_decoded(
        self,
    ) -> dict[str, Any]:
        """Get map set and decoded subsets diagnostics."""
        raw = await self.async_raw_get_map_set()

        data = raw.get("resp", {}).get("body", {}).get("data", {})
        subsets = data.get("subsets")

        response: dict[str, Any] = {
            "map_set_summary": {
                "mid": data.get("mid"),
                "msid": data.get("msid"),
                "type": data.get("type"),
                "infoSize": data.get("infoSize"),
            },
            "raw": raw,
        }

        if isinstance(subsets, str) and subsets:
            response["decoded_subsets"] = _decode_map_subsets_payload(subsets)
        else:
            response["decoded_subsets"] = {"error": "no_subsets_payload"}

        return response

    async def async_raw_get_quick_commands(
        self,
    ) -> dict[str, Any]:
        """Get available quick commands from the device."""
        async with self._quick_commands_lock:
            _LOGGER.debug("Fetching quick commands for %s", self.entity_id)

            if self._capability.custom is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="quick_commands_not_supported",
                )

            try:
                dev_info = await self._device.execute_command(
                    self._capability.custom.set(
                        "getInfo",
                        {"data": "getRobotState"},
                    )
                )

                map_id = (
                    dev_info
                    .get("resp", {})
                    .get("body", {})
                    .get("data", {})
                    .get("getRobotState", {})
                    .get("data", {})
                    .get("mid")
                )

                if not map_id:
                    raise KeyError("mid missing from getRobotState response")

                result = await self._device.execute_command(
                    self._capability.custom.set(
                        "getQuickCommand",
                        {"mid": map_id},
                    )
                )

                if not isinstance(result, dict):
                    raise TypeError("Invalid quick command response")

                data = result.get("resp", {}).get("body", {}).get("data")
                commands: list[Any] = []
                if isinstance(data, list) and data:
                    first = data[0]
                    if isinstance(first, dict):
                        commands = first.get("array", [])

                if not isinstance(commands, list):
                    commands = []

                quick_commands: list[dict[str, Any]] = []
                for command in commands:
                    if not isinstance(command, dict):
                        continue

                    name = command.get("name")
                    qcid = command.get("qcid")
                    command_mid = command.get("mid")

                    if not name or not qcid or not command_mid:
                        continue

                    quick_commands.append(
                        {
                            "name": name,
                            "qcid": qcid,
                            "mid": command_mid,
                        }
                    )

                self._quick_commands = quick_commands

                _LOGGER.debug(
                    "Quick commands for %s: %s",
                    self.entity_id,
                    quick_commands,
                )

                return {
                    "mid": map_id,
                    "quick_commands": quick_commands,
                    "raw": result,
                }
            except Exception as err:
                _LOGGER.debug(
                    "Quick commands not supported for %s: %s",
                    self.entity_id,
                    err,
                )
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="quick_commands_not_supported",
                ) from err

    async def _async_ensure_fallback_rooms(self) -> bool:
        """Populate fallback room list from decoded map set when RoomsEvent is missing."""
        if self._room_event is not None or self._fallback_rooms:
            return False

        try:
            decoded_response = await self.async_raw_get_map_set_decoded()
        except ServiceValidationError:
            return False
        except Exception as err:  # pragma: no cover - defensive fallback
            _LOGGER.debug("Unable to load fallback rooms from map set: %s", err)
            return False

        decoded_subsets = decoded_response.get("decoded_subsets", {})
        decoded_rooms = decoded_subsets.get("rooms")
        fallback_rooms = _extract_rooms_from_decoded_payload(decoded_rooms)
        if not fallback_rooms:
            return False

        self._fallback_rooms = fallback_rooms
        map_id = decoded_response.get("map_set_summary", {}).get("mid")
        self._fallback_map_id = str(map_id) if map_id is not None else None
        return True

    def _get_fallback_command_id_for_segment(
        self,
        segment_id: str,
    ) -> int | float | str | None:
        """Map a fallback segment id to the command id expected by the device."""
        for room in self._fallback_rooms:
            if str(room.get("id")) == segment_id:
                command_id = room.get("command_id", room.get("id"))
                if isinstance(command_id, (int, float)):
                    return command_id
                if isinstance(command_id, str):
                    return command_id
                return None

        return None
    # END CUSTOM CODE

    @callback
    def _check_segments_changed(self) -> None:
        """Check if segments have changed and create repair issue."""
        last_seen = self.last_seen_segments
        if last_seen is None:
            return

        last_seen_ids = {seg.id for seg in last_seen}
        current_ids = {seg.id for seg in self._get_segments()}

        if current_ids != last_seen_ids:
            self.async_create_segments_issue()

    def _get_segments(self) -> list[Segment]:
        """Get the segments that can be cleaned."""
        last_seen = self.last_seen_segments or []
        if not self._maps:
            # If we don't have the necessary information to determine segments, return the last
            # seen segments to avoid temporarily losing all segments until we get the necessary
            # information, which could cause unnecessary issues to be created
            return last_seen

        room_entries: list[tuple[int, str]] = []
        map_id: str | None = None

        if self._room_event is not None:
            map_id = self._room_event.map_id
            room_entries = [(room.id, room.name) for room in self._room_event.rooms]
        elif self._fallback_rooms:
            map_id = self._fallback_map_id or next(
                (map_obj.id for map_obj in self._maps.values() if map_obj.using),
                None,
            )
            room_entries = [
                (int(room["id"]), str(room["name"]))
                for room in self._fallback_rooms
                if "id" in room and "name" in room
            ]

        if map_id is None:
            return last_seen

        if (map_obj := self._maps.get(map_id)) is None:
            _LOGGER.warning("Map ID %s not found in available maps", map_id)
            return []

        id_prefix = f"{map_id}{_SEGMENTS_SEPARATOR}"
        current_map_id = self._room_event.map_id if self._room_event is not None else map_id
        other_map_ids = {
            map_obj.id
            for map_obj in self._maps.values()
            if map_obj.id != current_map_id
        }
        # Include segments from the current map and any segments from other maps that were
        # previously seen, as we want to continue showing segments from other maps for
        # mapping purposes
        segments = [
            seg for seg in last_seen if _split_composite_id(seg.id)[0] in other_map_ids
        ]
        segments.extend(
            Segment(
                id=f"{id_prefix}{room_id}",
                name=room_name,
                group=map_obj.name,
            )
            for room_id, room_name in room_entries
        )
        return segments

    async def async_get_segments(self) -> list[Segment]:
        """Get the segments that can be cleaned."""
        return self._get_segments()

    async def async_clean_segments(self, segment_ids: list[str], **kwargs: Any) -> None:
        """Perform an area clean.

        Only cleans segments from the currently selected map.
        """
        if not self._maps:
            _LOGGER.warning("No map information available, cannot clean segments")
            return

        valid_room_ids: list[int | float | str] = []
        fallback_map_id = self._fallback_map_id or next(
            (map_obj.id for map_obj in self._maps.values() if map_obj.using),
            None,
        )
        fallback_mode = self._room_event is None and bool(self._fallback_rooms)

        for composite_id in segment_ids:
            map_id, segment_id = _split_composite_id(composite_id)

            if fallback_mode and fallback_map_id is not None and map_id == fallback_map_id:
                command_id = self._get_fallback_command_id_for_segment(segment_id)
                if command_id is None:
                    _LOGGER.warning(
                        "Fallback segment ID %s not found in decoded rooms", segment_id
                    )
                    continue

                valid_room_ids.append(command_id)
                continue

            if (map_obj := self._maps.get(map_id)) is None:
                _LOGGER.warning("Map ID %s not found in available maps", map_id)
                continue

            if not map_obj.using:
                room_name = next(
                    (
                        segment.name
                        for segment in self.last_seen_segments or []
                        if segment.id == composite_id
                    ),
                    "",
                )
                _LOGGER.warning(
                    'Map "%s" is not currently selected, skipping segment "%s" (%s)',
                    map_obj.name,
                    room_name,
                    segment_id,
                )
                continue

            valid_room_ids.append(int(segment_id))

        if not valid_room_ids:
            _LOGGER.warning(
                "No valid segments to clean after validation, skipping clean segments command"
            )
            return

        if TYPE_CHECKING:
            # Supported feature is only added if clean.action.area is not None
            assert self._capability.clean.action.area is not None

        if fallback_mode:
            rooms_str = ";".join(f"1,{room_id}" for room_id in valid_room_ids)
            _LOGGER.debug(
                "Using clean_V2 command for fallback rooms (X11): %s",
                rooms_str,
            )
            await self._device.execute_command(
                self._capability.custom.set(
                    "clean_V2",
                    {
                        "act": "start",
                        "content": {
                            "type": "freeClean",
                            "value": rooms_str,
                        },
                    },
                )
            )
            return

        try:
            await self._device.execute_command(
                self._capability.clean.action.area(
                    CleanMode.SPOT_AREA,
                    valid_room_ids,
                    1,
                )
            )
            return
        except Exception as err:  # noqa: BLE001
            # Some models (e.g. X11) report "rcp not support" for clean.area.
            if "rcp not support" not in str(err).lower() or self._capability.custom is None:
                raise

            _LOGGER.warning(
                "clean.area rejected (%s), retrying via custom spot_area command",
                err,
            )

        await self._device.execute_command(
            self._capability.custom.set(
                "spot_area",
                {
                    "rooms": valid_room_ids,
                    "cleanings": 1,
                },
            )
        )


@callback
def _split_composite_id(composite_id: str) -> tuple[str, str]:
    """Split a composite ID into its components."""
    map_id, _, segment_id = composite_id.partition(_SEGMENTS_SEPARATOR)
    return map_id, segment_id
