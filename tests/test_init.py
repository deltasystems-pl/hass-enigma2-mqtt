"""Smoke test: the integration loads on top of the MQTT integration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.enigma2_mqtt.const import DOMAIN


async def test_setup(hass: HomeAssistant, mqtt_mock) -> None:
    """Setting up the domain succeeds and registers the component."""
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    assert DOMAIN in hass.config.components
