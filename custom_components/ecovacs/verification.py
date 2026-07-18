"""Helpers for Ecovacs device verification."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from typing import Any
from urllib.parse import urljoin

from aiohttp import ClientTimeout, hdrs
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from deebot_client.authentication import RestConfiguration
from deebot_client.exceptions import AuthenticationError, InvalidAuthenticationError
from deebot_client.util import md5

_LOGGER = logging.getLogger(__name__)

_CLIENT_KEY = "1520391301804"
_CLIENT_SECRET = "6c319b2a5cd3e66e39159c2e28f2fce9"  # noqa: S105
_PRIVATE_API_PATH_FORMAT = "/v1/private/{country}/{lang}/{deviceId}/{appCode}/{appVersion}/{channel}/{deviceType}/{endpoint}"
_PUBLIC_KEY_CONFIG = "PUBLIC.KEY.CONFIG"
_META = {
    "lang": "EN",
    "appCode": "global_e",
    "appVersion": "3.14.0",
    "channel": "google_play",
    "deviceType": "1",
}
_ANDROID_MODEL = "Pixel 7"
_ANDROID_SYSTEM = "Android 14"
_TIMEOUT = ClientTimeout(60)


class DeviceVerificationRequiredError(AuthenticationError):
    """Device verification is required before authentication."""


class InvalidVerificationCodeError(InvalidAuthenticationError):
    """Invalid or expired device verification code."""


def is_device_verification_required_error(ex: Exception) -> bool:
    """Return True if the exception indicates Ecovacs device verification is required."""
    return isinstance(ex, AuthenticationError) and (
        "failure code 1013" in str(ex)
        or "Please update to the latest version to continue" in str(ex)
    )


class EcovacsVerificationClient:
    """Client for the Ecovacs email device-verification flow."""

    def __init__(self, config: RestConfiguration, account_id: str) -> None:
        """Initialize the verification client."""
        self._config = config
        self._account_id = account_id
        self._meta: dict[str, str] = {
            **_META,
            "country": self._config.country.lower(),
            "deviceId": self._config.device_id,
        }
        self._public_key: rsa.RSAPublicKey | None = None

    async def request_device_verification_code(self) -> None:
        """Request a one-time email code for the configured device ID."""
        encrypted_email = await self._encrypt_account(self._account_id)
        await self._call_private_api(
            "user/sendEmailVerifyCode",
            {
                "encryptEmail": encrypted_email,
                "verifyType": "EMAIL_VERIFY_DEVICE",
                "supportChar": "N",
                "isForce": "N",
                **self._request_metadata(),
            },
        )

    async def verify_device(self, verification_code: str) -> None:
        """Verify the configured device ID using the one-time email code."""
        encrypted_account = await self._encrypt_account(self._account_id)
        await self._call_private_api(
            "user/verifyDevice",
            {
                "encryptAccount": encrypted_account,
                "backUpEmail": "",
                "verifyCode": verification_code.strip(),
                "model": _ANDROID_MODEL,
                "system": _ANDROID_SYSTEM,
                **self._request_metadata(),
            },
        )

    async def _call_private_api(self, endpoint: str, params: dict[str, str | int]) -> Any:
        """Call a signed Ecovacs private API endpoint."""
        url = urljoin(
            self._config.login_url,
            _PRIVATE_API_PATH_FORMAT.format(endpoint=endpoint, **self._meta),
        )
        return await self._do_auth_response(url, self._sign(params, self._meta))

    async def _do_auth_response(self, url: str, params: dict[str, Any]) -> Any:
        async with self._config.session.get(url, params=params, timeout=_TIMEOUT) as res:
            res.raise_for_status()

            content_type = res.headers.get(hdrs.CONTENT_TYPE, "").lower()
            response = await res.json(content_type=content_type)
            _LOGGER.debug("got response code %s for %s", response.get("code"), url)

            if response["code"] == "0000":
                return response["data"]
            if response["code"] in ["1005", "1010"]:
                raise InvalidAuthenticationError(response["msg"])
            if response["code"] == "1012":
                raise InvalidVerificationCodeError(response["msg"])
            if response["code"] == "1013":
                raise DeviceVerificationRequiredError(response["msg"])

            _LOGGER.error("call to %s failed with %s", url, response)
            msg = f"failure code {response['code']} ({response['msg']}) for call {url}"
            raise AuthenticationError(msg)

    @staticmethod
    def _request_metadata() -> dict[str, str | int]:
        now = time.time()
        return {
            "requestId": md5(str(now)),
            "authTimespan": int(now * 1000),
            "authTimeZone": "GMT-8",
        }

    async def _get_public_key(self) -> rsa.RSAPublicKey:
        if self._public_key is not None:
            return self._public_key

        response = await self._call_private_api(
            "common/getConfig",
            {"keys": _PUBLIC_KEY_CONFIG, **self._request_metadata()},
        )
        if not isinstance(response, list):
            raise AuthenticationError("Invalid public key configuration response")

        for entry in response:
            if not isinstance(entry, dict) or entry.get("key") != _PUBLIC_KEY_CONFIG:
                continue
            value = entry.get("value")
            if not isinstance(value, str):
                break
            try:
                config = json.loads(value)
                encoded_key = config["publicKey"]
            except (KeyError, TypeError, json.JSONDecodeError) as ex:
                raise AuthenticationError("Invalid Ecovacs public key") from ex
            if not isinstance(encoded_key, str):
                raise AuthenticationError("Invalid Ecovacs public key")
            try:
                key = serialization.load_der_public_key(
                    base64.b64decode(encoded_key, validate=True)
                )
            except (binascii.Error, TypeError, ValueError) as ex:
                raise AuthenticationError("Invalid Ecovacs public key") from ex
            if not isinstance(key, rsa.RSAPublicKey):
                raise AuthenticationError("Ecovacs public key is not an RSA key")
            self._public_key = key
            return key

        raise AuthenticationError("Ecovacs public key configuration is missing")

    async def _encrypt_account(self, account: str) -> str:
        public_key = await self._get_public_key()
        encrypted = public_key.encrypt(account.encode(), padding.PKCS1v15())
        return base64.b64encode(encrypted).decode()

    @staticmethod
    def _sign(
        params: dict[str, str | int],
        additional_sign_params: dict[str, str | int],
    ) -> dict[str, str | int]:
        sign_data: dict[str, str | int] = {**additional_sign_params, **params}
        sign_on_text = (
            _CLIENT_KEY
            + "".join(f"{k}={sign_data[k]}" for k in sorted(sign_data.keys()))
            + _CLIENT_SECRET
        )
        signed_params = {**params}
        signed_params["authSign"] = md5(sign_on_text)
        signed_params["authAppkey"] = _CLIENT_KEY
        return signed_params
