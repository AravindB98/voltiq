"""DBC-driven CAN codec.

Wraps :mod:`cantools` so every component (simulator, ingestion, tests) shares
one source of truth for the CAN matrix — exactly how production BMS/telematics
stacks stay in sync with the vehicle's DBC release.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
from pathlib import Path

import cantools
from cantools.database.can import Database

DEFAULT_DBC = "voltiq_bms.dbc"


def _bundled_dbc_path() -> Path:
    return Path(str(resources.files("voltiq").joinpath("data", DEFAULT_DBC)))


@lru_cache(maxsize=4)
def load_database(dbc_path: str | None = None) -> Database:
    path = Path(dbc_path) if dbc_path else _bundled_dbc_path()
    return cantools.database.load_file(str(path))


class DbcCodec:
    """Encode/decode CAN frames against a DBC file."""

    def __init__(self, dbc_path: str | None = None) -> None:
        self.db = load_database(dbc_path)
        self._by_id = {msg.frame_id: msg for msg in self.db.messages}

    @property
    def message_names(self) -> list[str]:
        return [m.name for m in self.db.messages]

    def encode(self, message_name: str, signals: dict[str, float]) -> tuple[int, bytes]:
        """Encode physical signal values into (arbitration_id, payload)."""
        msg = self.db.get_message_by_name(message_name)
        return msg.frame_id, msg.encode(signals, strict=True)

    def decode(self, arbitration_id: int, data: bytes) -> tuple[str, dict[str, float]] | None:
        """Decode a raw frame. Returns None for IDs not in the DBC.

        Unknown IDs are expected on a real bus (other ECUs chatter constantly),
        so this is a soft failure by design.
        """
        msg = self._by_id.get(arbitration_id)
        if msg is None:
            return None
        decoded = msg.decode(data, decode_choices=False)
        return msg.name, {k: float(v) for k, v in decoded.items()}
