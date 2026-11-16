import sys
from unittest.mock import AsyncMock, patch
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from custom_components.microair_easytouch.coordinator import MicroAirBLECoordinator, parse_zone_config

def info(mode): return [68, 76, 74, 70, 72, 45, 0, 128, 128, 128, mode, 128, 75, 255, 0, 0]

@pytest.fixture(autouse=True)
def auto_enable(enable_custom_integrations):
    yield

async def test_ble_entry_sets_up_entities(hass):
    entry = MockConfigEntry(domain="microair_easytouch", title="RV", data={
        "connection_type": "bluetooth", "mac_address": "AA:BB:CC:DD:EE:FF",
        "email": "", "password": "", "name": "RV", "zone_count": 2})
    entry.add_to_hass(hass)

    async def fake_update(self):
        self.zone_configs[0] = parse_zone_config(0, {"CFG": {"MA": [1, 2, 4, 7, 11]}})
        self.zone_configs[1] = parse_zone_config(1, {"CFG": {"MA": [1, 2, 4]}})
        return self._parse_all_zones({"Z_sts": {"0": info(7), "1": info(2)}, "PRM": []})

    with patch.object(MicroAirBLECoordinator, "_async_update_data", fake_update), \
         patch("homeassistant.setup.async_process_deps_reqs", AsyncMock()), \
         patch.object(MicroAirBLECoordinator, "_send_command", AsyncMock(return_value=True)) as send:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        ids = sorted(hass.states.async_entity_ids())
        print(ids)
        s0 = hass.states.get("select.rv_zone_1_heat_source")
        assert s0.state == "Heat pump / heat strip"
        assert s0.attributes["options"] == ["Furnace", "Heat pump / heat strip"]
        assert hass.states.get("select.rv_zone_2_heat_source").attributes["options"] == ["Furnace"]
        assert hass.states.get("climate.rv_zone_1").state == "heat"
        await hass.services.async_call("climate", "set_hvac_mode",
            {"entity_id": "climate.rv_zone_2", "hvac_mode": "off"}, blocking=True)
        assert send.await_args.args[0]["Changes"] == {"zone": 1, "mode": 0}
        await hass.services.async_call("button", "press",
            {"entity_id": "button.rv_system_off"}, blocking=True)
        assert send.await_args.args[0]["Changes"] == {"zone": 0, "power": 0}
        assert await hass.config_entries.async_unload(entry.entry_id)
