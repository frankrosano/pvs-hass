"""Support for PVS gateway."""

from __future__ import annotations
from typing import Any

from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import PVSUpdateCoordinator


class PVSBaseEntity(CoordinatorEntity[PVSUpdateCoordinator]):
    """Defines a base PVS entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PVSUpdateCoordinator,
        description: EntityDescription,
    ) -> None:
        """Init the PVS base entity."""
        self.entity_description = description
        serial_number = coordinator.pvs.serial_number
        assert serial_number is not None
        self.pvs_serial_num = serial_number
        super().__init__(coordinator)

    @property
    def data(self) -> dict[str, Any]:
        """Return PVS data."""
        data = self.coordinator.pvs.data
        assert data is not None
        return data
