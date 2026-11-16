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

## Verification

- Final affected Python suite: **363 passed**, with **224 passing subtests**.
  The existing manager/fork test emits one multiprocessing deprecation warning.
- **100 non-browser Galaxy JavaScript test files passed.** The patched dashboard
  also passed real headless Chrome checks at 416 and 1280 pixel widths: observed
  distance and partial-coverage labels render without overflow or browser errors.
  The separate upstream FLM browser harness assumes POSIX paths and did not run
  successfully on this Windows host; the dashboard browser checks use a local
  fixture and the actual patched Home component instead.
- Regression coverage includes registered Sonata parked/live scope agreement,
  different devices/firmware/reset generations, missing identity refusal,
  355-segment admission and 513-segment refusal, damaged/interior/cancelled logs,
  durable partial statistics, replacement after repair, exclusions, HTTP access,
  controller replay, persistence and shutdown.
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
it is not a new full native build or a road-tested steering calibration.

## First validation after installation

Keep Auto Learn enabled for the selected vehicle, with no manually owned FLM
surface and Safe Mode off. Record a new drive on this build. Leave the comma
powered after ignition off so the logger can close the recording and the parked
worker can analyze it. Galaxy should show current analysis, counts and either a
specific refusal or a provisional next-drive result. A new tune still requires
sufficient eligible evidence and native qualification; enabling learning alone
does not guarantee a nonzero adjustment. Older incomplete recordings can supply
diagnostics and observed dashboard statistics, not fabricated tuning proof.
