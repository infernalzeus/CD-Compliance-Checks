"""Device registry.

Maps a device key -> its processor. Actigraph is fully implemented; the other
CHiP-D devices are registered as `NotImplementedDevice` placeholders so the CLI
can list them, discovery stays inert for them, and adding one later is a single
line change here plus a new processor module.
"""
from __future__ import annotations

from .actigraph import ActigraphProcessor
from .base import DeviceProcessor, NotImplementedDevice

# Folder names as they appear inside each season directory.
_PENDING = {
    "expiwell": "Expiwell",
    "saliva": "Saliva",
    "cognitron": "Cognitron",
    "qualtrics": "Qualtrics",
    "mieye": "MiEYE",
}

_REGISTRY: dict[str, DeviceProcessor] = {
    "actigraph": ActigraphProcessor(),
}
for _key, _folder in _PENDING.items():
    _REGISTRY[_key] = NotImplementedDevice(_key, _folder)


def get_processor(device: str) -> DeviceProcessor:
    key = device.lower()
    if key not in _REGISTRY:
        raise KeyError(
            f"Unknown device '{device}'. Known: {', '.join(sorted(_REGISTRY))}"
        )
    return _REGISTRY[key]


def implemented_devices() -> list[str]:
    return [
        k
        for k, v in _REGISTRY.items()
        if not isinstance(v, NotImplementedDevice)
    ]


def all_devices() -> list[str]:
    return sorted(_REGISTRY)
