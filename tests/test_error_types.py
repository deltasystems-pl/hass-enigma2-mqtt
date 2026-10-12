"""Which error type a refused call raises, and what a caller gets for it.

`errors.py` has the rule: a refusal - the moment or the request was wrong, nothing was
done - is a `ServiceValidationError`, a failure a plain `HomeAssistantError`. The tests of
each button, select and card assert the type beside the sentence they already assert;
this file holds what is common to them: the helper, the installer's list of refusals,
and what the two types turn into on Home Assistant's two APIs.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.setup import async_setup_component
import homeassistant.util.dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enigma2_mqtt.box import Enigma2CommandError, Enigma2CommandRefused
from custom_components.enigma2_mqtt.const import DOMAIN
from custom_components.enigma2_mqtt.errors import service_error
from custom_components.enigma2_mqtt.installer import REFUSALS, InstallerErrorCode
from custom_components.enigma2_mqtt.release_store import RESULT_FAILED, CheckRateLimited

from .conftest import async_setup_box

CHECK = "button.dekoder_salon_check_for_plugin_updates"
CACHE = "custom_components.enigma2_mqtt.button.async_release_index_cache"


def test_the_helper_picks_the_type_and_keeps_the_sentence() -> None:
    refusal = service_error(True, "update_downgrade")
    failure = service_error(False, "update_failed", {"reason": "install_failed"})

    assert type(refusal) is ServiceValidationError
    assert type(failure) is HomeAssistantError
    assert (refusal.translation_domain, refusal.translation_key) == (DOMAIN, "update_downgrade")
    assert failure.translation_key == "update_failed"
    assert failure.translation_placeholders == {"reason": "install_failed"}


def test_a_refused_command_is_still_a_command_error() -> None:
    """Everything that catches a refusal of the receiver's catches the coded one too."""
    assert issubclass(Enigma2CommandRefused, Enigma2CommandError)
    assert issubclass(Enigma2CommandRefused, ServiceValidationError)
    assert not issubclass(Enigma2CommandError, ServiceValidationError)


def test_the_installer_s_refusals_are_exactly_these() -> None:
    """Spelled out, so that a code joins or leaves the list by a decision.

    A refusal is a code every raise of which means: the moment or the request was wrong,
    and the receiver is as it was. Everything else - a receiver that cannot be reached,
    an install or a restart that failed, a rollback, a state of the receiver's own - is
    a failure.
    """
    assert {code.name for code in REFUSALS} == {
        "RECORDING",
        "TIMER_DUE",
        "STANDBY",
        "STREAMING",
        "BUSY",
        "BUSY_STALLED",
        "BUSY_UNTIL",
        "OPKG_BUSY",
        "NEWER_INSTALLED",
        "BUNDLE_BELOW_FLOOR",
        "BUNDLE_WITHDRAWN",
        "VERSION_BELOW_FLOOR",
        "VERSION_WITHDRAWN",
        "VERSION_NOT_INSTALLABLE",
    }
    assert all(isinstance(code, InstallerErrorCode) for code in REFUSALS)


def _rate_limited() -> Any:
    limited = CheckRateLimited(dt_util.utcnow() - timedelta(minutes=3), 420.0)
    return SimpleNamespace(async_check=AsyncMock(side_effect=limited))


def _failing() -> Any:
    result = SimpleNamespace(outcome=RESULT_FAILED, error="timeout")
    return SimpleNamespace(async_check=AsyncMock(return_value=result))


@pytest.mark.parametrize(
    ("cache", "code", "key"),
    [
        (_rate_limited, "service_validation_error", "release_check_rate_limited"),
        (_failing, "home_assistant_error", "release_check_failed"),
    ],
    ids=["a refusal", "a failure"],
)
async def test_the_websocket_api_says_which_it_was_with_the_translation_key(
    hass: HomeAssistant,
    hass_ws_client,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    cache: Any,
    code: str,
    key: str,
) -> None:
    """What the frontend is told: the code it words the toast by, and the sentence's key."""
    await async_setup_box(hass, config_entry)
    client = await hass_ws_client(hass)

    with patch(CACHE, return_value=cache()):
        await client.send_json_auto_id(
            {
                "type": "call_service",
                "domain": "button",
                "service": "press",
                "service_data": {"entity_id": CHECK},
            }
        )
        answer = await client.receive_json()

    assert answer["success"] is False
    assert answer["error"]["code"] == code
    assert answer["error"]["translation_domain"] == DOMAIN
    assert answer["error"]["translation_key"] == key


@pytest.mark.parametrize("cache", [_rate_limited, _failing], ids=["a refusal", "a failure"])
async def test_the_rest_api_answers_500_for_either_type(
    hass: HomeAssistant,
    hass_client,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    cache: Any,
) -> None:
    """Home Assistant's own behaviour, pinned because the documentation states it.

    The REST API turns only a schema error and an unknown service into a client error;
    any `HomeAssistantError`, a `ServiceValidationError` included, is answered 500 by the
    web server. No error type an integration can raise changes that. When this test
    fails, Home Assistant has changed, and DOCUMENTATION.md section 5 and `errors.py`
    say something that is no longer true.
    """
    assert await async_setup_component(hass, "api", {})
    await async_setup_box(hass, config_entry)
    client = await hass_client()

    with patch(CACHE, return_value=cache()):
        response = await client.post("/api/services/button/press", json={"entity_id": CHECK})

    assert response.status == 500
