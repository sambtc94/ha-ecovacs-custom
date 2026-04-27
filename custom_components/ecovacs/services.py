"""Ecovacs services."""

from __future__ import annotations

from homeassistant.components.vacuum import DOMAIN as VACUUM_DOMAIN
from homeassistant.core import HomeAssistant, SupportsResponse, callback
from homeassistant.helpers import service

from .const import DOMAIN

SERVICE_RAW_GET_POSITIONS = "raw_get_positions"
# BEGIN CUSTOM CODE
SERVICE_RAW_GET_MAP_INFO = "raw_get_map_info"
SERVICE_RAW_GET_MAP_SET = "raw_get_map_set"
SERVICE_RAW_GET_MAP_SET_DECODED = "raw_get_map_set_decoded"
SERVICE_RAW_GET_QUICK_COMMANDS = "raw_get_quick_commands"
# END CUSTOM CODE


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services."""

    # Vacuum Services
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_RAW_GET_POSITIONS,
        entity_domain=VACUUM_DOMAIN,
        schema=None,
        func="async_raw_get_positions",
        supports_response=SupportsResponse.ONLY,
    )

    # BEGIN CUSTOM CODE
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_RAW_GET_MAP_INFO,
        entity_domain=VACUUM_DOMAIN,
        schema=None,
        func="async_raw_get_map_info",
        supports_response=SupportsResponse.ONLY,
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_RAW_GET_MAP_SET,
        entity_domain=VACUUM_DOMAIN,
        schema=None,
        func="async_raw_get_map_set",
        supports_response=SupportsResponse.ONLY,
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_RAW_GET_MAP_SET_DECODED,
        entity_domain=VACUUM_DOMAIN,
        schema=None,
        func="async_raw_get_map_set_decoded",
        supports_response=SupportsResponse.ONLY,
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_RAW_GET_QUICK_COMMANDS,
        entity_domain=VACUUM_DOMAIN,
        schema=None,
        func="async_raw_get_quick_commands",
        supports_response=SupportsResponse.ONLY,
    )
    # END CUSTOM CODE
