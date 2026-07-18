"""VoltIQ command-line interface.

Commands:
    voltiq demo      simulate a fleet, ingest it, run analytics (end-to-end seed)
    voltiq simulate  simulate one vehicle into the database
    voltiq analyze   run the analytics engine over stored telemetry
    voltiq serve     start the API + dashboard
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from voltiq.analytics.engine import AnalyticsEngine
from voltiq.decoder.dbc import DbcCodec
from voltiq.ingest.pipeline import IngestPipeline
from voltiq.ingest.store import Store
from voltiq.simulator.vehicle import FaultScenario, VehicleSimulator

DEFAULT_DB = "voltiq.db"
BATCH = 5_000

# Demo fleet: healthy vehicles plus two fault scenarios the analytics must catch
DEMO_FLEET = [
    dict(vin="5YJ3E1EA0PF000001", daily_km=45, fault=None),
    dict(vin="5YJ3E1EA0PF000002", daily_km=70, fault=None),
    dict(vin="5YJ3E1EA0PF000003", daily_km=120, fault=None),  # heavy user: faster fade
    dict(vin="5YJ3E1EA0PF000004", daily_km=55,
         fault=dict(kind="weak_cell", frac=0.6)),             # weak cell mid-life
    dict(vin="5YJ3E1EA0PF000005", daily_km=60,
         fault=dict(kind="cooling_degraded", frac=0.5)),      # cooling failure
]


def _simulate_into(store: Store, vin: str, days: int, daily_km: float,
                   seed: int, fault: FaultScenario | None) -> int:
    codec = DbcCodec()
    pipeline = IngestPipeline(store, codec)
    start = time.time() - days * 86_400
    sim = VehicleSimulator(vin, codec, start_time=start, daily_km=daily_km,
                           seed=seed, fault=fault)
    batch: list[tuple[str, float, int, bytes]] = []
    total = 0
    for frame in sim.frames(days=days):
        batch.append((frame.vin, frame.timestamp, frame.arbitration_id, frame.data))
        if len(batch) >= BATCH:
            total += pipeline.ingest(batch).decoded
            batch.clear()
    if batch:
        total += pipeline.ingest(batch).decoded
    return total


def cmd_demo(args: argparse.Namespace) -> int:
    store = Store(args.db)
    fleet = DEMO_FLEET[: args.vehicles]
    for i, v in enumerate(fleet):
        fault = None
        if v["fault"]:
            fault = FaultScenario(kind=v["fault"]["kind"],
                                  start_day=int(args.days * v["fault"]["frac"]))
        n = _simulate_into(store, v["vin"], args.days, v["daily_km"], seed=i, fault=fault)
        print(f"  simulated {v['vin']}: {n:,} frames"
              + (f"  [fault: {fault.kind} @ day {fault.start_day}]" if fault else ""))
    print("running analytics …")
    for r in AnalyticsEngine(store).run_all():
        print(f"  {r.vin}: status={r.status:8s} soh={r.soh_pct or '—'}%"
              f" rul={r.rul_cycles or '—'} cycles  alerts={r.new_alerts}"
              f" anomaly_rate={r.anomaly_rate_pct or '—'}%")
    print(f"done. {store.frame_count():,} telemetry rows in {args.db}")
    print("next: voltiq serve   → http://localhost:8000")
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    store = Store(args.db)
    fault = FaultScenario(args.fault, start_day=args.fault_day) if args.fault else None
    n = _simulate_into(store, args.vin, args.days, args.daily_km, args.seed, fault)
    print(f"simulated {args.vin}: {n:,} frames -> {args.db}")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    store = Store(args.db)
    for r in AnalyticsEngine(store).run_all():
        print(f"{r.vin}: status={r.status} soh={r.soh_pct}% alerts={r.new_alerts}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from voltiq.api.app import create_app
    uvicorn.run(create_app(args.db), host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="voltiq", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="seed a demo fleet end-to-end")
    d.add_argument("--db", default=DEFAULT_DB)
    d.add_argument("--vehicles", type=int, default=5, choices=range(1, 6))
    d.add_argument("--days", type=int, default=365)
    d.set_defaults(fn=cmd_demo)

    s = sub.add_parser("simulate", help="simulate one vehicle")
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--vin", default="5YJ3E1EA0PF999999")
    s.add_argument("--days", type=int, default=180)
    s.add_argument("--daily-km", type=float, default=55.0)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--fault", choices=["weak_cell", "cooling_degraded"])
    s.add_argument("--fault-day", type=int, default=90)
    s.set_defaults(fn=cmd_simulate)

    a = sub.add_parser("analyze", help="run analytics over stored telemetry")
    a.add_argument("--db", default=DEFAULT_DB)
    a.set_defaults(fn=cmd_analyze)

    v = sub.add_parser("serve", help="start API + dashboard")
    v.add_argument("--db", default=DEFAULT_DB)
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8000)
    v.set_defaults(fn=cmd_serve)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
