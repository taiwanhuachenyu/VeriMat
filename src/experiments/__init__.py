"""Fully automated closed-loop evaluation for the GOAI semifinal experiment.

The package adds what the survey pipeline alone does not have:

- :mod:`.budget`  — a hard token budget enforced at the transport boundary;
- :mod:`.claims`  — the claim unit every method variant emits and every score reads;
- :mod:`.oracle`  — the time-split validation-window oracle that replaces expert review;
- :mod:`.methods` — the preregistered method variants (baselines, ablations, full system);
- :mod:`.scoring` — deterministic metrics, calibration, cost accounting and statistics.

Nothing here touches a human in the loop: ground truth comes from validation-window literature
retrieved through the same audited Sciverse client, plus optional composition databases.
"""
