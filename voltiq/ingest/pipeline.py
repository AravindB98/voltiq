"""Ingestion pipeline: raw CAN frames -> decode -> validate -> persist.

Single choke-point through which all telemetry enters the system, whether it
arrives over HTTP (`api.app`) or from the bulk simulator loader (`cli`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from voltiq.decoder.dbc import DbcCodec
from voltiq.ingest.store import Store

log = logging.getLogger(__name__)


@dataclass
class IngestStats:
    accepted: int = 0
    decoded: int = 0
    unknown_ids: int = 0


class IngestPipeline:
    def __init__(self, store: Store, codec: DbcCodec | None = None) -> None:
        self.store = store
        self.codec = codec or DbcCodec()

    def ingest(self, frames: list[tuple[str, float, int, bytes]]) -> IngestStats:
        """frames: (vin, timestamp, arbitration_id, payload)"""
        stats = IngestStats(accepted=len(frames))
        rows: list[tuple[str, float, str, dict[str, float]]] = []
        for vin, ts, arb_id, data in frames:
            try:
                decoded = self.codec.decode(arb_id, data)
            except Exception:  # malformed payload for a known ID
                log.warning("undecodable frame vin=%s id=0x%X", vin, arb_id)
                stats.unknown_ids += 1
                continue
            if decoded is None:
                stats.unknown_ids += 1
                continue
            name, signals = decoded
            rows.append((vin, ts, name, signals))
        if rows:
            self.store.insert_telemetry(rows)
        stats.decoded = len(rows)
        return stats
