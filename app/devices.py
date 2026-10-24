"""The list of devices to watch, loaded from devices.yaml."""

import re
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# IP addresses (v4/v6) and host names. A leading "-" is refused because the address is
# passed to the ping command, where it would be read as an option.
_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:_-]*$")

Port = Annotated[int, Field(ge=1, le=65535)]


class DeviceConfigError(Exception):
    pass


class Device(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=80)
    ip: str = Field(max_length=253, description="IP address or host name")
    ping: bool = True
    ports: tuple[Port, ...] = ()

    @field_validator("ip")
    @classmethod
    def _check_host(cls, value: str) -> str:
        if not _HOST.match(value):
            raise ValueError(f"{value!r} is not a valid IP address or host name")
        return value

    @model_validator(mode="after")
    def _has_a_check(self) -> "Device":
        if not self.ping and not self.ports:
            raise ValueError(f"{self.name}: enable ping or list at least one port")
        return self

    @property
    def checks_label(self) -> str:
        parts = (["ping"] if self.ping else []) + [f"tcp/{p}" for p in self.ports]
        return " · ".join(parts)


class DevicesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    devices: list[Device] = Field(min_length=1)

    @field_validator("devices")
    @classmethod
    def _unique_names(cls, devices: list[Device]) -> list[Device]:
        seen: set[str] = set()
        for device in devices:
            if device.name in seen:
                raise ValueError(f"device name {device.name!r} is used twice")
            seen.add(device.name)
        return devices


def load_devices(path: Path) -> list[Device]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DeviceConfigError(f"cannot read {path}: {exc.strerror}") from exc
    except yaml.YAMLError as exc:
        raise DeviceConfigError(f"{path} is not valid YAML: {exc}") from exc
    try:
        return DevicesFile.model_validate(raw).devices
    except ValidationError as exc:
        raise DeviceConfigError(f"{path} has errors:\n{exc}") from exc
