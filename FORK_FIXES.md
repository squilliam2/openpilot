# Personal Dom fork: FLM and drive history

Based on [StarPilotHQ/StarPilot `Dom`](https://github.com/StarPilotHQ/StarPilot/tree/cfafb8ca72c6d39c70a29007e1c3049664433989).
Install branch: [`squilliam2/openpilot:dom`](https://github.com/squilliam2/openpilot/tree/dom).
Custom software URL: **https://installer.comma.ai/squilliam2/dom**.

## Findings and changes

1. **Unknown VIN prevented both recording a usable route scope and loading a
   parked configuration.** The supplied Galaxy export reported "Known vehicle
   and no manual coordinates required"; the recorded Sonata CP contains the
   unknown VIN sentinel and 12 firmware entries. Both live and parked paths now
   use one vehicle binding rule. A valid VIN keeps the existing rule. Without
   one, persistence requires a registered device ID and nonempty ECU firmware,
   and remains bound to the complete typed CP, saved settings, controller and
   baseline. No VIN is invented or written to the car.

   **Original VIN discovery is unchanged and still runs first.** The upstream
   VIN queries in `opendbc/car/vin.py` and vehicle discovery in `car_helpers.py`
   were never replaced. A discovered VIN always takes precedence over the new
   fallback. A regression test verifies that neither path rewrites CarParams.

   **Reset Auto Learn whenever moving this device to another vehicle, including
   an identical car.** The fallback is a device-local firmware binding; it cannot
   uniquely distinguish two physically different cars with identical CP/firmware.
   Missing registration/firmware still prevents persistence. Current calibration,
   native authority, source leases, manual tuning and explicit OFF checks remain.

2. **The 355-segment drive exceeded FLM's 180-segment limit.** The export's
   "Duplicate segment or route segment limit exceeded" was a size refusal, not
   evidence that segments were duplicated. FLM now matches the existing 512
   segment inventory limit. Its separate 2 GiB input cap is replaced by the
   admitted 512 × 32 MiB per-file bound. Readers still process one file at a time;
   cancellation, source revisions, complete-source checks and fitting bounds
   remain. Existing input-selected analysis handles long completed recordings.

3. **Galaxy admitted the route into inventory, then rejected its size.** The
   unchanged upstream analyzer returned "Route source changed or exceeds analysis
   limit" for the same 517,128,139 bytes of qlogs because its cap was 256 MiB.
   Galaxy now uses the same bounded segment/file budget, with a deadline scaled
   to the admitted segment count and cancellation while reading/decoding.

4. **A damaged final segment discarded the intact drive's statistics.** The
   downloaded last rlog is an unfinished Zstandard frame without `endOfRoute`.
   Dashboard analysis can now retain a contiguous, fully decoded earlier portion,
   explicitly labeled **Recorded portion only**. It never integrates the readable
   prefix of a damaged frame or bridges a missing interior segment. These
   statistics survive restart, contribute only observed distance/time, and are
   replaced rather than added again if the recording is repaired. Personal
   records use complete drives. Analysis version 6 retries older cached failures.

   Strict analysis remains the default for other callers. A partial dashboard
   result never becomes a completed FLM/trial receipt or accepted tune. Decoder
   cancellation cannot publish partial success.

5. **Existing recordings now have an independent historical review path.**
   Galaxy automatically queues existing local dense rlogs while parked, including
   recordings older than the optimizer's eight-route input selection. Under
   **Steering → Activity → Existing routes**, request a local rerun or paste a
   specific Connect route ID/URL for this comma. Connect uses the device's existing
   comma authentication for its inventory; it does not send that authentication to
   log storage. Cloud downloads happen only on explicit request while parked.

   Each review selects at most 14 evenly spread original segments, including the
   first and last. Cloud input is capped at 448 MiB, requires 512 MiB free space,
   and is removed afterward. Reports record segment coverage and omissions,
   low/high-speed tracking error and error reversals, controller metadata and route
   scope availability. They can be downloaded as JSON. The review library retains
   up to 100 records; bounded inventory/coverage is disclosed in the UI.

   These are **observational reports, not historical tune application**. Legacy
   recordings without configuration/pose/native evidence cannot become qualified
   tuning receipts. Unclosed or damaged compressed segments are omitted, not
   repaired. Missing original rlogs cannot be reconstructed from sparse qlogs.
   Reviews use a separate bounded, low-priority child, leave logger identity and
   tuning Params untouched, and pause/reap when the vehicle starts. Reports persist
   across restart; source changes trigger another review. Removed automatic queue
   entries cannot fill the library and block newer recordings.

6. **Optional live FLM is wired into the managed process.** Upstream's process
   launcher ran the parked daemon; onroad it recorded for later analysis. Its
   parked route scope also selected the parked overlay in Controls, bypassing the
   existing live tracking service. Choosing **Live FLM · experimental** now starts
   the existing `steerlearnerd` live learner on the next drive and leaves the parked
   overlay inactive. Returning offroad stops/reaps live children and replaces the
   process image before any parked work. Only the selected learner runs.

   This uses the existing physical-response qualification, native/source leases,
   controller eligibility, bounded proposals, gradual application and rollback.
   It does not change the optimizer math, steering safety limits or driver override.
   Confirmed persistence uses the same VIN-first/device-firmware identity binding.
   Insufficient evidence, disabled learning, manual ownership or a failed trial
   still prevents an update. Live learning is not a promise that every FLM value
   changes on every drive, and it does not alter the separate LAGD estimator.

   **Parked mode remains the default.** The per-vehicle mode is an optional field
   in the existing preferences document, so no new native Params key or ARM binary
   is needed. Switching mode preserves the existing On/Off choice. Before rolling
   back to older software, choose Parked for every platform with Live selected:
   clearing the last Live choice removes the optional field. An older strict
   reader otherwise rejects it and disables learning until preferences are reset.

## Verification

- Final affected Python regression suite: **867 passed**, with **258 passing subtests**.
  Existing tests emit one
  multiprocessing warning and three Cap'n Proto repeated-write warnings.
- **102 non-browser Galaxy JavaScript test files passed.** The patched dashboard
  also passed real headless Chrome checks at 416 and 1280 pixel widths: observed
  distance and partial-coverage labels render without overflow or browser errors.
  The separate upstream FLM browser harness assumes POSIX paths and did not run
  successfully on this Windows host; the dashboard browser checks use a local
  fixture and the actual patched Home component instead. Separate Chrome checks
  also exercise the actual new route-review and learning-mode components at both
  widths, including queuing a Connect review and saving Live mode through local
  API fixtures. No browser errors or horizontal overflow were observed.
- Regression coverage includes registered Sonata parked/live scope agreement,
  different devices/firmware/reset generations, missing identity refusal,
  355-segment admission and 513-segment refusal, damaged/interior/cancelled logs,
  durable partial statistics, replacement after repair, exclusions, HTTP access,
  controller replay, persistence and shutdown.
- Live-mode integration with a discovered VIN reaches the actual Controls
  application path, applies only the existing bounded correction, then withdraws
  it on driver override. Mode selection is parked-only, checks the vehicle/settings
  revision, and preserves explicit OFF and the original saved CarParams.
- Eight intact dense rlogs sampled from the supplied drive produce **84
  uninterrupted tracking windows** across both speed cohorts in the real bounded
  review subprocess, despite unknown VIN and missing route scope. The unfinished
  final segment is omitted. This is sampled diagnostic coverage, not a full-route
  tune or proof of its cause.
- The patched analyzer processed all 355 qlogs from the supplied drive: **536,749
  meters (333.5 miles), 21,235.775 seconds, 15.3% normal engagement and 75.6% AOL
  steering-only time**, excluding the damaged final segment. Upstream returned
  no distance/date/duration for that identical input.
- Actual recorded CP also passes the new parked/live binding smoke check.
  Settings marked `DONT_LOG` cannot be reconstructed from route logs; the smoke
  check uses their unset defaults. They must not be mistaken for empty settings
  actually saved on the device.
- Shutdown fixture writes are now explicitly synchronous before cancellation;
  this removes a race in the upstream test, rather than changing tune storage.

No private route files, location data, device IDs or Galaxy export are included
in this repository. This is a Python/web patch using the upstream ARM binaries;
it is not a new full native build or a road-tested steering calibration. Device
authentication/download failure paths are tested with API fixtures; authenticated
Connect import and live performance still need validation on the actual comma.

## First validation after installation

1. Keep Auto Learn enabled for the selected vehicle, with no manually owned FLM
   surface and Safe Mode off. Choosing a learning mode does not turn Auto Learn on.
2. For updates during a drive, open **Galaxy → Steering → Overview → When Auto
   Learn runs** while parked. Select **Live FLM · experimental**, then **Save for
   next drive**. The next drive uses qualified live updates; sufficient evidence
   and native qualification are still required. Validate this experimental mode
   on the actual vehicle before relying on its behavior.
3. For the original workflow, keep **Parked analysis · next-drive tune**. Record
   a new drive and leave the comma powered after ignition off so the logger can
   close it and the worker can analyze it. Galaxy should show counts and either
   a specific refusal or a provisional next-drive result.
4. Existing local recordings are reviewed automatically while parked. For a
   particular cloud route, use **Steering → Activity → Existing routes** and
   explicitly request it. Dashboard statistics remain a separate analysis path;
   observed partial-drive statistics and historical reports are never substituted
   for completed tuning proof.
