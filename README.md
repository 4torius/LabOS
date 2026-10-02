# LabOS data archive

Data and analysis scripts kept out of LabOS itself. This branch shares no
history with `main`.

- `Plots/` — spectra plots of the SPAAC / DA hydrogel plate-reader runs
  (Sept 2026), made by `scripts/plot_spectra.py` and
  `scripts/associate_recipe_measurements.py`.
- `robot_reliability/` — the mobile robot base-offset reliability map
  (journal, trials, tasks, summary), made by `scripts/robot_reliability_runner.py`;
  procedure in `docs/ROBOT_RELIABILITY_TEST_IT.md`.

The scripts expect to be run from a LabOS checkout (they talk to its REST API
or read its `Results/` folders); paths in them are the ones they had there.
