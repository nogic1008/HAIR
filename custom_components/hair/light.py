"""Light entity platform for HAIR."""
from __future__ import annotations

import logging
import math
from typing import Any

from homeassistant.components.light import (
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.color import brightness_to_value, value_to_brightness

from .const import DOMAIN, DeviceType
from .models import IRDevice

_LOGGER = logging.getLogger(__name__)

BRIGHTNESS_STEPS_DEFAULT = 10
COLOR_TEMP_STEPS_DEFAULT = 10
COLOR_TEMP_MIN_DEFAULT = 2700
COLOR_TEMP_MAX_DEFAULT = 6500
ATTR_BRIGHTNESS = "brightness"
ATTR_COLOR_TEMP_KELVIN = "color_temp_kelvin"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    device_manager = data["device_manager"]
    factory = data["entity_factory"]

    entities: dict[str, HAIRLightEntity] = {}

    @callback
    def _on_add(device: IRDevice) -> None:
        if device.device_type != DeviceType.LIGHT:
            return
        if device.id in entities:
            return
        entity = HAIRLightEntity(device, device_manager)
        entities[device.id] = entity
        async_add_entities([entity])

    @callback
    def _on_remove(device_id: str) -> None:
        entity = entities.pop(device_id, None)
        if entity is not None:
            hass.async_create_task(entity.async_remove())

    @callback
    def _on_update(device: IRDevice) -> None:
        entity = entities.get(device.id)
        if entity is not None:
            entity.update_device(device)

    factory.register_platform_hooks(
        "light", on_add=_on_add, on_remove=_on_remove, on_update=_on_update
    )
    factory.register_platform("light", async_add_entities)

    for device in device_manager.get_all_devices():
        _on_add(device)


class HAIRLightEntity(LightEntity):
    """IR-controlled light."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_assumed_state = True

    def __init__(self, device: IRDevice, device_manager) -> None:
        self._device = device
        self._manager = device_manager
        self._attr_unique_id = f"hair_{device.id}_light"
        self._attr_name = None
        self._is_on = False
        self._brightness_value: int | None = None
        self._color_temp_kelvin: int | None = None

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._device.id)},
            "name": self._device.name,
            "manufacturer": self._device.manufacturer or "HAIR",
            "model": self._device.model or "Light",
        }

    @property
    def color_mode(self) -> ColorMode:
        if self._has_color_temp_control:
            return ColorMode.COLOR_TEMP
        if self._has_brightness_control:
            return ColorMode.BRIGHTNESS
        return ColorMode.ONOFF

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        return {self.color_mode}

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def brightness(self) -> int | None:
        if self._brightness_value is None:
            return None
        return value_to_brightness(
            self._brightness_scale,
            self._brightness_value,
        )

    @property
    def color_temp_kelvin(self) -> int | None:
        return self._color_temp_kelvin

    @property
    def min_color_temp_kelvin(self) -> int:
        cfg = self._device.entity_config.color_temp_min_kelvin
        if cfg is not None and cfg > 0:
            return cfg
        return COLOR_TEMP_MIN_DEFAULT

    @property
    def max_color_temp_kelvin(self) -> int:
        cfg = self._device.entity_config.color_temp_max_kelvin
        if cfg is not None and cfg > 0:
            return cfg
        return COLOR_TEMP_MAX_DEFAULT

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._send("turn_on", "power_toggle")
        self._is_on = True

        if ATTR_BRIGHTNESS in kwargs and kwargs[ATTR_BRIGHTNESS] is not None:
            await self._apply_brightness(int(kwargs[ATTR_BRIGHTNESS]))

        if (
            ATTR_COLOR_TEMP_KELVIN in kwargs
            and kwargs[ATTR_COLOR_TEMP_KELVIN] is not None
        ):
            await self._apply_color_temp_kelvin(
                int(kwargs[ATTR_COLOR_TEMP_KELVIN]),
            )

        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send("turn_off", "power_toggle")
        self._is_on = False
        self.async_write_ha_state()

    @property
    def _has_brightness_control(self) -> bool:
        mapping = self._device.entity_config.command_mapping
        return "brightness_up" in mapping or "brightness_down" in mapping

    @property
    def _has_color_temp_control(self) -> bool:
        mapping = self._device.entity_config.command_mapping
        return (
            "color_temp_warmer" in mapping
            or "color_temp_cooler" in mapping
        )

    def _brightness_step_count(self) -> int:
        cfg = self._device.entity_config.brightness_steps
        if cfg is not None and cfg > 0:
            return cfg
        return BRIGHTNESS_STEPS_DEFAULT

    def _color_temp_step_count(self) -> int:
        cfg = self._device.entity_config.color_temp_steps
        if cfg is not None and cfg > 0:
            return cfg
        return COLOR_TEMP_STEPS_DEFAULT

    @property
    def _brightness_scale(self) -> tuple[int, int]:
        steps = self._brightness_step_count()
        if steps <= 1:
            return (1, 1)
        return (1, steps)

    def _color_temp_levels(self) -> list[int]:
        steps = self._color_temp_step_count()
        min_k = self.min_color_temp_kelvin
        max_k = self.max_color_temp_kelvin
        if steps <= 1 or min_k >= max_k:
            return [min_k]
        return [
            round(min_k + (i * (max_k - min_k) / (steps - 1)))
            for i in range(steps)
        ]

    @staticmethod
    def _nearest_index(levels: list[int], value: int) -> int:
        return min(
            range(len(levels)),
            key=lambda idx: abs(levels[idx] - value),
        )

    async def _apply_brightness(self, target: int) -> None:
        target = max(1, min(255, target))
        min_value, max_value = self._brightness_scale
        target_value = math.ceil(
            brightness_to_value(self._brightness_scale, target)
        )
        target_value = max(min_value, min(max_value, target_value))
        current_value = (
            self._brightness_value
            if self._brightness_value is not None
            else min_value
        )
        delta = target_value - current_value

        if self._has_brightness_control:
            if delta > 0:
                for _ in range(delta):
                    if not await self._send("brightness_up"):
                        break
            elif delta < 0:
                for _ in range(abs(delta)):
                    if not await self._send("brightness_down"):
                        break

        self._brightness_value = target_value

    async def _apply_color_temp_kelvin(self, target: int) -> None:
        levels = self._color_temp_levels()
        target = max(self.min_color_temp_kelvin, min(self.max_color_temp_kelvin, target))

        target_idx = self._nearest_index(levels, target)
        current_value = (
            self._color_temp_kelvin
            if self._color_temp_kelvin is not None
            else levels[0]
        )
        current_idx = self._nearest_index(levels, current_value)
        delta = target_idx - current_idx

        if self._has_color_temp_control:
            if delta > 0:
                for _ in range(delta):
                    if not await self._send("color_temp_cooler"):
                        break
            elif delta < 0:
                for _ in range(abs(delta)):
                    if not await self._send("color_temp_warmer"):
                        break

        self._color_temp_kelvin = levels[target_idx]

    @callback
    def update_device(self, device: IRDevice) -> None:
        self._device = device
        if self.hass is None:
            # Race: entity instantiated and tracked in the platform's local
            # dict but not yet registered with HA via async_add_entities.
            # The state from __init__ is correct; HA writes it once the
            # registration coroutine completes.
            return
        self.async_write_ha_state()

    async def _send(self, *feature_keys: str) -> bool:
        mapping = self._device.entity_config.command_mapping
        for key in feature_keys:
            command_name = mapping.get(key)
            if command_name is None:
                continue
            command = self._device.get_command_by_name(command_name)
            if command is not None:
                await self._manager.async_send_command(
                    self._device.id, command.id
                )
                return True
        _LOGGER.warning(
            "No mapped IR command on %s for features %s",
            self._device.name,
            feature_keys,
        )
        return False
