# Contributing to VoltIQ

Thanks for your interest! VoltIQ aims to be the clearest open reference implementation of an EV battery health pipeline — contributions that improve realism, robustness, or clarity are all welcome.

⭐ Starring and 🍴 forking the repo are the easiest ways to support the project.

## Getting set up

```bash
git clone https://github.com/<your-fork>/voltiq.git
cd voltiq
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -v && ruff check .        # everything should be green before you start
```

## Workflow

1. Open or comment on an issue first for anything non-trivial, so we agree on direction before you invest time.
2. Create a feature branch: `git checkout -b feat/short-description`.
3. Keep the contract: every behaviour change needs a test, and `pytest -v`, `ruff check .`, and `voltiq demo` must all pass (CI enforces this on Python 3.10, 3.11 and 3.12).
4. Open a pull request explaining **what** changed and **why**. Small, focused PRs merge fastest.

## Where help is most valuable

* **Battery realism**: LFP/NCA OCV curves, calendar aging, low-temperature lithium-plating behaviour, charge-taper (CC-CV) refinement.
* **Analytics**: Kalman/EKF SOC estimation, incremental-capacity (dQ/dV) analysis, smarter RUL models, fleet-level cross-vehicle baselines.
* **Fault scenarios**: stuck sensors, contactor faults, BMS communication dropouts, thermal-runaway precursor signatures.
* **Infrastructure**: TimescaleDB/ClickHouse storage backend behind the existing `Store` interface, MQTT/Kafka ingestion, alert webhooks.
* **Docs & examples**: notebooks walking through the SOH/RUL math, real-world DBC adaptation guides.

## Code style

* Python 3.10+, type hints on public APIs, docstrings that explain *why* not just *what*.
* `ruff` (line length 100) is the single source of formatting truth.
* No new runtime dependencies without discussion — zero-service reproducibility is a core feature.

## Questions

Open an issue at https://github.com/AravindB98/voltiq/issues or email aravindo2011@gmail.com.
