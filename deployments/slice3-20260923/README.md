# Slice 3 live candidate — 2026-09-23

This branch records the exact 336-file deployed source candidate. The deployment manifest lists SHA-256 hashes for every file; `marker_positioner.py` is the sole change from the preceding diagnostic deployment.

The deadline-order regression and focused positioner checks passed locally. The one live startup retry confirmed one recovery index and then stopped with `marker index deadline exceeded`. The sorter was safely returned to standby. Slice 3 is **not live qualified**.
