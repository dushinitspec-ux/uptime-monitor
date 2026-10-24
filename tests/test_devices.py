from pathlib import Path

import pytest

from app.devices import DeviceConfigError, load_devices

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "devices.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("name", ["devices.yaml", "devices.demo.yaml"])
def test_shipped_device_files_are_valid(name: str) -> None:
    assert load_devices(PROJECT_ROOT / name)


def test_defaults(tmp_path: Path) -> None:
    devices = load_devices(write(tmp_path, "devices:\n  - name: Router\n    ip: 10.0.0.1\n"))
    assert devices[0].ping is True
    assert devices[0].ports == ()
    assert devices[0].checks_label == "ping"


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        ("devices:\n  - {name: A, ip: 10.0.0.1}\n  - {name: A, ip: 10.0.0.2}\n", "used twice"),
        ("devices:\n  - {name: A, ip: 10.0.0.1, ports: [70000]}\n", "less than or equal"),
        ("devices:\n  - {name: A, ip: '-c 1000'}\n", "not a valid IP"),
        ("devices:\n  - {name: A, ip: 10.0.0.1, ping: false}\n", "enable ping"),
        ("devices:\n  - {name: A, ip: 10.0.0.1, prot: [80]}\n", "Extra inputs"),
        ("devices: []\n", "at least 1"),
        ("devices: [\n", "not valid YAML"),
    ],
)
def test_invalid_files(tmp_path: Path, yaml_text: str, message: str) -> None:
    with pytest.raises(DeviceConfigError, match=message):
        load_devices(write(tmp_path, yaml_text))


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(DeviceConfigError, match="cannot read"):
        load_devices(tmp_path / "nope.yaml")
