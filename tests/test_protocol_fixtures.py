"""Offline checks for the captured EasyTouch protocol values."""
import importlib.util
import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).parents[1]
CONST_PATH = ROOT / "custom_components/microair_easytouch/const.py"


def load_const():
    spec = importlib.util.spec_from_file_location("easytouch_const", CONST_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ProtocolFixtureTests(unittest.TestCase):
    def test_mode_numbers(self):
        const = load_const()
        self.assertEqual(const.BLE_MODE_NUM_TO_HA[5], "heat")
        self.assertEqual(const.BLE_MODE_NUM_TO_HA[10], "heat_cool")
        self.assertEqual(const.BLE_MODE_NUM_TO_HA[11], "auto")
        self.assertEqual(const.BLE_EQUIPMENT_MODE_NUM_TO_NAME[11], "auto_gas")

    def test_fan_matrix(self):
        const = load_const()
        self.assertEqual(
            {value: const.BLE_FAN_MODE_DEVICE_TO_HA[value] for value in (1, 2, 3, 65, 66, 67, 128)},
            {
                1: "low",
                2: "medium",
                3: "high",
                65: "cycled_low",
                66: "cycled_medium",
                67: "cycled_high",
                128: "auto",
            },
        )

    def test_equipment_mode_labels_and_ble_platform(self):
        const = load_const()
        self.assertEqual(const.PLATFORMS_BLE, ["climate", "sensor", "button", "select"])
        self.assertEqual(
            const.BLE_EQUIPMENT_MODE_LABELS,
            {
                "gas": "Gas",
                "heat_pump": "Heat Pump",
                "auto_gas": "Auto Gas",
                "auto_heat_pump": "Auto Heat Pump",
            },
        )

    def test_fixture_json_and_zone_shapes(self):
        for name in (
            "diagnostic-auto-hp-zone-1-3.json",
            "diagnostic-cool-cycled-medium-fan-only-low.json",
            "diagnostic-device-profile.json",
            "diagnostic-equipment-modes.json",
            "diagnostic-fan-codes-and-gas.json",
            "diagnostic-status-points.json",
        ):
            with self.subTest(name=name):
                with (ROOT / name).open() as stream:
                    json.load(stream)

        with (ROOT / "diagnostic-auto-hp-zone-1-3.json").open() as stream:
            capture = json.load(stream)
        self.assertEqual(set(capture["zones"]), {"0", "2"})
        self.assertEqual(
            [capture["zones"][key]["physical_zone"] for key in ("0", "2")],
            [1, 3],
        )


if __name__ == "__main__":
    unittest.main()
