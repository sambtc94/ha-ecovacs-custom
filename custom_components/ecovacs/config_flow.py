"""Config flow for Ecovacs mqtt integration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
import logging
import ssl
from typing import Any
from urllib.parse import urlparse

from aiohttp import ClientError
from deebot_client.authentication import Authenticator, RestConfiguration, create_rest_config
from deebot_client.const import UNDEFINED, UndefinedType
from deebot_client.exceptions import AuthenticationError, InvalidAuthenticationError, MqttError
from deebot_client.mqtt_client import MqttClient, create_mqtt_config
from deebot_client.util import md5
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_COUNTRY, CONF_MODE, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client, selector
from homeassistant.helpers.typing import VolDictType
from homeassistant.util.ssl import get_default_no_verify_context

from .const import (
    CONF_DEVICE_ID,
    CONF_OVERRIDE_MQTT_URL,
    CONF_OVERRIDE_REST_URL,
    CONF_VERIFICATION_CODE,
    CONF_VERIFY_MQTT_CERTIFICATE,
    DOMAIN,
    InstanceMode,
)
from .util import generate_client_device_id
from .verification import (
    EcovacsVerificationClient,
    InvalidVerificationCodeError,
    is_device_verification_required_error,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ValidationResult:
    """Validation result."""

    errors: dict[str, str]
    requires_device_verification: bool = False


def _validate_url(
    value: str,
    field_name: str,
    schema_list: set[str],
) -> dict[str, str]:
    """Validate an URL and return error dictionary."""
    if urlparse(value).scheme not in schema_list:
        return {field_name: f"invalid_url_schema_{field_name}"}
    try:
        vol.Schema(vol.Url())(value)
    except vol.Invalid:
        return {field_name: "invalid_url"}
    return {}


def _is_self_hosted(user_input: Mapping[str, Any]) -> bool:
    """Return True if the flow is using a self-hosted Ecovacs instance."""
    return CONF_OVERRIDE_REST_URL in user_input


def _get_device_id(hass: HomeAssistant, user_input: Mapping[str, Any]) -> str:
    """Return the stored or generated device id for the flow."""
    return user_input.get(CONF_DEVICE_ID) or generate_client_device_id(
        hass, _is_self_hosted(user_input)
    )


def _create_rest_configuration(
    hass: HomeAssistant, user_input: Mapping[str, Any], device_id: str
) -> RestConfiguration:
    """Create rest configuration for the current flow."""
    return create_rest_config(
        aiohttp_client.async_get_clientsession(hass),
        device_id=device_id,
        alpha_2_country=user_input[CONF_COUNTRY],
        override_rest_url=user_input.get(CONF_OVERRIDE_REST_URL),
    )


def _create_ecovacs_authenticator(
    hass: HomeAssistant, user_input: Mapping[str, Any], device_id: str
) -> Authenticator:
    """Create an authenticator for the current flow."""
    return Authenticator(
        _create_rest_configuration(hass, user_input, device_id),
        user_input[CONF_USERNAME],
        md5(user_input[CONF_PASSWORD]),
    )


async def _validate_input(
    hass: HomeAssistant, user_input: dict[str, Any], device_id: str
) -> ValidationResult:
    """Validate user input."""
    errors: dict[str, str] = {}

    if rest_url := user_input.get(CONF_OVERRIDE_REST_URL):
        errors.update(
            _validate_url(rest_url, CONF_OVERRIDE_REST_URL, {"http", "https"})
        )
    if mqtt_url := user_input.get(CONF_OVERRIDE_MQTT_URL):
        errors.update(
            _validate_url(mqtt_url, CONF_OVERRIDE_MQTT_URL, {"mqtt", "mqtts"})
        )

    if errors:
        return ValidationResult(errors)

    country = user_input[CONF_COUNTRY]
    authenticator = _create_ecovacs_authenticator(hass, user_input, device_id)

    try:
        await authenticator.authenticate()
    except ClientError:
        _LOGGER.debug("Cannot connect", exc_info=True)
        return ValidationResult({"base": "cannot_connect"})
    except InvalidAuthenticationError:
        return ValidationResult({"base": "invalid_auth"})
    except AuthenticationError as ex:
        if not _is_self_hosted(user_input) and is_device_verification_required_error(ex):
            return ValidationResult({}, requires_device_verification=True)
        _LOGGER.exception("Unexpected authentication exception during login")
        return ValidationResult({"base": "unknown"})
    except Exception:
        _LOGGER.exception("Unexpected exception during login")
        return ValidationResult({"base": "unknown"})

    mqtt_url = user_input.get(CONF_OVERRIDE_MQTT_URL)
    ssl_context: UndefinedType | ssl.SSLContext = UNDEFINED
    if not user_input.get(CONF_VERIFY_MQTT_CERTIFICATE, True) and mqtt_url:
        ssl_context = get_default_no_verify_context()

    mqtt_config = await hass.async_add_executor_job(
        partial(
            create_mqtt_config,
            device_id=device_id,
            country=country,
            override_mqtt_url=mqtt_url,
            ssl_context=ssl_context,
        )
    )

    client = MqttClient(mqtt_config, authenticator)
    cannot_connect_field = CONF_OVERRIDE_MQTT_URL if mqtt_url else "base"

    try:
        await client.verify_config()
    except MqttError:
        _LOGGER.debug("Cannot connect", exc_info=True)
        errors[cannot_connect_field] = "cannot_connect"
    except InvalidAuthenticationError:
        errors["base"] = "invalid_auth"
    except Exception:
        _LOGGER.exception("Unexpected exception during mqtt connection verification")
        errors["base"] = "unknown"

    return ValidationResult(errors)


class EcovacsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Ecovacs."""

    VERSION = 1

    _mode: InstanceMode = InstanceMode.CLOUD

    def __init__(self) -> None:
        """Initialize verification state used across config and reauth steps."""
        self._pending_user_input: dict[str, Any] | None = None
        self._pending_device_id: str | None = None
        self._reauth_entry: ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        if not self.show_advanced_options:
            return await self.async_step_auth()

        if user_input:
            self._mode = user_input[CONF_MODE]
            return await self.async_step_auth()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MODE, default=InstanceMode.CLOUD
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=list(InstanceMode),
                            translation_key="installation_mode",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            last_step=False,
        )

    async def async_step_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the auth step."""
        errors: dict[str, str] = {}

        if user_input:
            if self._reauth_entry is None:
                self._async_abort_entries_match(
                    {CONF_USERNAME: user_input[CONF_USERNAME]}
                )
            device_id = _get_device_id(self.hass, user_input)
            validation = await _validate_input(self.hass, user_input, device_id)

            if validation.requires_device_verification:
                self._pending_user_input = {**user_input, CONF_DEVICE_ID: device_id}
                self._pending_device_id = device_id
                request_errors = await self._async_request_device_verification_code()
                if not request_errors:
                    return await self.async_step_verify_device()
                errors = request_errors
            else:
                errors = validation.errors
                if not errors:
                    if self._reauth_entry is not None:
                        return self.async_update_reload_and_abort(
                            self._reauth_entry,
                            data_updates=user_input,
                        )
                    return self.async_create_entry(
                        title=user_input[CONF_USERNAME],
                        data={**user_input, CONF_DEVICE_ID: device_id},
                    )

        return self._show_auth_form(user_input, errors)

    def _show_auth_form(
        self, user_input: dict[str, Any] | None, errors: dict[str, str]
    ) -> ConfigFlowResult:
        """Show the Ecovacs auth form."""
        schema: VolDictType = {
            vol.Required(CONF_USERNAME): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
            ),
            vol.Required(CONF_PASSWORD): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Required(CONF_COUNTRY): selector.CountrySelector(),
        }
        if self._mode == InstanceMode.SELF_HOSTED:
            schema.update(
                {
                    vol.Required(CONF_OVERRIDE_REST_URL): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                    ),
                    vol.Required(CONF_OVERRIDE_MQTT_URL): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                    ),
                }
            )
            if errors:
                schema[vol.Optional(CONF_VERIFY_MQTT_CERTIFICATE, default=True)] = bool

        if not user_input:
            user_input = {
                CONF_COUNTRY: self.hass.config.country,
            }

        data_schema = vol.Schema(schema)
        if user_input:
            data_schema = self.add_suggested_values_to_schema(
                data_schema=data_schema, suggested_values=user_input
            )

        return self.async_show_form(
            step_id="auth",
            data_schema=data_schema,
            errors=errors,
            last_step=self._reauth_entry is None,
        )

    async def async_step_verify_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle device verification."""
        errors: dict[str, str] = {}

        if self._pending_user_input is None or self._pending_device_id is None:
            return await self.async_step_auth()

        if user_input:
            try:
                verifier = EcovacsVerificationClient(
                    _create_rest_configuration(
                        self.hass, self._pending_user_input, self._pending_device_id
                    ),
                    self._pending_user_input[CONF_USERNAME],
                )
                await verifier.verify_device(user_input[CONF_VERIFICATION_CODE])
            except ClientError:
                _LOGGER.debug("Cannot connect", exc_info=True)
                errors["base"] = "cannot_connect"
            except InvalidVerificationCodeError:
                errors["base"] = "invalid_verification_code"
            except InvalidAuthenticationError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected exception during device verification")
                errors["base"] = "unknown"
            else:
                validation = await _validate_input(
                    self.hass, self._pending_user_input, self._pending_device_id
                )
                if validation.errors:
                    if validation.requires_device_verification:
                        errors["base"] = "verification_required"
                    else:
                        return self._show_auth_form(
                            self._pending_user_input, validation.errors
                        )
                else:
                    if self._reauth_entry is not None:
                        return self.async_update_reload_and_abort(
                            self._reauth_entry,
                            data_updates={
                                **self._pending_user_input,
                                CONF_DEVICE_ID: self._pending_device_id,
                            },
                        )
                    return self.async_create_entry(
                        title=self._pending_user_input[CONF_USERNAME],
                        data={
                            **self._pending_user_input,
                            CONF_DEVICE_ID: self._pending_device_id,
                        },
                    )

        return self.async_show_form(
            step_id="verify_device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_VERIFICATION_CODE): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.TEXT
                        )
                    )
                }
            ),
            errors=errors,
            description_placeholders={
                "email": self._pending_user_input[CONF_USERNAME],
                "device_id": self._pending_device_id,
            },
            last_step=True,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication."""
        self._reauth_entry = self._get_reauth_entry()
        self._mode = (
            InstanceMode.SELF_HOSTED
            if CONF_OVERRIDE_REST_URL in entry_data
            else InstanceMode.CLOUD
        )
        self._pending_user_input = dict(entry_data)
        self._pending_device_id = _get_device_id(self.hass, self._pending_user_input)
        self._pending_user_input[CONF_DEVICE_ID] = self._pending_device_id

        request_errors = await self._async_request_device_verification_code()
        if request_errors:
            return await self.async_step_auth(self._pending_user_input)

        return await self.async_step_verify_device()

    async def _async_request_device_verification_code(self) -> dict[str, str]:
        """Request a one-time verification code for the pending flow."""
        if self._pending_user_input is None or self._pending_device_id is None:
            return {"base": "unknown"}

        try:
            verifier = EcovacsVerificationClient(
                _create_rest_configuration(
                    self.hass, self._pending_user_input, self._pending_device_id
                ),
                self._pending_user_input[CONF_USERNAME],
            )
            await verifier.request_device_verification_code()
        except ClientError:
            _LOGGER.debug("Cannot connect", exc_info=True)
            return {"base": "cannot_connect"}
        except InvalidAuthenticationError:
            return {"base": "invalid_auth"}
        except Exception:
            _LOGGER.exception("Unexpected exception during verification code request")
            return {"base": "unknown"}

        return {}
