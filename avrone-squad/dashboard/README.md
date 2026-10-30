# AVRONE Dashboard

## Harmonic Thermo Gauge (proposal D)
D3 radial gauge in static/index.html. Polls /api/codex/events every 2s, worst-lane entropy debt from restart-cost blocks. Green 0.0-0.3 grounded, amber 0.3-0.7 abstract, red 0.7-1.0 literal. Zero new routes.

## Smoke (proposal E)
`tests/test_harmonic_smoke.py` serves injected index on loopback, asserts container/d3/script markers. `python -m pytest tests/test_harmonic_smoke.py -q`.
