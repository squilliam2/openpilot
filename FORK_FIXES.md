# Personal Dom fork: FLM and drive history

Updated through [upstream `Dom` 8688217e10f2](https://github.com/StarPilotHQ/StarPilot/commit/8688217e10f2d17321c6e660393d054e2911bb34).
The original investigation used upstream `cfafb8ca72c6`.
Install branch: [`squilliam2/openpilot:dom`](https://github.com/squilliam2/openpilot/tree/dom).
Custom software URL: **https://raw.githubusercontent.com/squilliam2/openpilot/dom/installer-dom**.
The older `installer.comma.ai/squilliam2/dom` endpoint currently fails on the
tested AGNOS 20.5.2 comma four before cloning; use the replacement above.

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
   records use complete drives. Analysis version 7 retries older cached failures,
   including locked/empty-tail inventory failures.

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

6. **Live + parked finishing is now the default for supported torque vehicles.** Upstream's process
   launcher ran the parked daemon; onroad it recorded for later analysis. Its
   parked route scope also selected the parked overlay in Controls, bypassing the
   existing live tracking service. The default now starts `steerlearnerd` during
   the drive and leaves the relative parked overlay inactive. Returning offroad
   stops/reaps live children and replaces the process image before parked work.
   A separate low-priority child finishes fitting the recorded drive. It has a
   2 GiB address-space limit, 300 CPU-second limit and 330-second parent deadline;
   the parent checks fresh parked state and reaps it when driving starts. Long
   completed drives use 14 input-selected original segments, including first/last,
   with source hashes and coverage retained. Unchanged completed fits and decode
   failures are memoized; recording, code, vehicle, settings or confirmed-history
   changes cause a retry. Cancelled work is not marked successfully processed.

   Parked fitting separates actually applied canonical profiles and requires
   agreement from disjoint training/held-out windows before queuing a bounded
   next-drive candidate. The candidate only prioritizes a direction among NEW
   live choices that already pass current native/probe requirements. Its old
   scores never become the new comparison baseline or future outcomes. Scope,
   reset, controller, baseline and currently accepted coordinates must match.
   Probe priority expires after 60 seconds so an unsupported preference cannot
   monopolize collection. No parked candidate writes confirmed live history or
   installs a second controller map. Actual application/acceptance still requires
   fresh live evidence, the existing independent trial and real Controls feedback.

   **An interrupted experiment cannot be confirmed while parked.** Finishing
   its numerical analysis is different from proving its real driving outcome.
   Already confirmed live values retain their original persistence/restore path;
   new parked suggestions remain provisional until the next drive qualifies them.

   This uses the existing physical-response qualification, native/source leases,
   controller eligibility, bounded proposals, gradual application and rollback.
   It does not change the optimizer math, steering safety limits or driver override.
   Confirmed persistence uses the same VIN-first/device-firmware identity binding.
   Insufficient evidence, disabled learning, manual ownership or a failed trial
   still prevents an update. Live learning is not a promise that every FLM value
   changes on every drive, and it does not alter the separate LAGD estimator.

   **Offroad only** remains an explicit persistent per-platform choice in Galaxy.
   Both mode choices preserve the independent Auto Learn On/Off setting. Missing
   or invalid vehicle data falls back to parked operation; explicit OFF, manual
   ownership and Safe Mode still prevent tuning. The existing optional preferences
   field needs no new native Params key. An upstream reader that rejects this
   field needs that preference document migrated before reverting software.

   The live protocol remains its original **30 coordinates**. The five additional
   relative friction-curve settings belong to the **Offroad only** route-map
   engine. They are not silently converted into live coordinates or combined with
   a live trial. Switching modes does not migrate accepted values between these
   different mathematical/application protocols.

7. **Driver input excludes the affected section, not the whole log segment.**
   Steering, brake, regen and accelerator flags are collected from original
   vehicle rows, including input pulses between otherwise valid tracking joins.
   Intervals retain the existing **350 ms before / 1 s after** exclusion buffer.
   A final source-clock pass removes overlapping 2 s or 6 s analysis windows even
   when an input occurs in the next segment. Unaffected windows before and after
   remain available; no gaps are interpolated and partial windows are not counted
   as complete ones. Live override still withdraws/rolls back an interrupted
   experiment, while completed clean baseline observations remain available.
   Detection depends on the vehicle's recorded input flags; unreported light
   steering contact cannot be inferred from a recording.

8. **The current upstream revision string could stop evidence recording.**
   Upstream's new `selected-steering-compatibility-v1:` plus SHA-256 exceeds
   `learning_wire`'s generic 96-byte string limit. Actual Controls integration
   failed to publish otherwise valid learning evidence, affecting both live and
   parked collection. Only the controller-revision field now uses the existing
   **192-byte** protocol bound, with matching read/write validation and bounded
   immutable key sharing. Other field limits are unchanged. Tests exercise the
   actual upstream-length revision and refuse a 193-byte value.

   The upstream merge also retains asynchronous command decoding, selected
   steering compatibility, larger local libraries, bounded partial-drive review,
   and its vehicle/migration fixes. The FLM CI workflow now includes `dom` and the
   new handoff, wire, driver-input and UI tests.

9. **Indoor power cannot finish a file interrupted by the earlier power loss.**
   The logger writes its normal route ending while shutting down; immediate loss
   of power can prevent that ending and leave a lock, an empty directory, or an
   unfinished compressed frame. Rebooting indoors does not replay the missing
   shutdown. The live parked finisher previously rejected these tails during
   inventory, before its decoder could consider usable earlier sections.

   The finisher now retains the exact logger route identity across that reboot
   and recovers independently closed, fully decoded sections. Missing route-end
   markers also produce partial coverage when every available segment closed
   normally. No route ending is fabricated and no recording or lock is modified.
   Partial review selects at most 32 evenly spread closed segments; live parked
   finishing uses at most 14. This sampling budget is independent of the 512
   segment full-route admission limit. Coverage, omissions and the last intact
   monotonic timestamp are reported; that cutoff is not an ignition-off time.

   Recovered live fits can queue a candidate for fresh native qualification and
   an independent live trial on the next drive. They do not turn an interrupted
   trial into confirmed history or directly install accepted steering values.
   Windows near an unobserved boundary are omitted conservatively: 1 s at the
   leading edge and 350 ms at the trailing edge, with entire overlapping windows
   discarded. Driver input elsewhere still excludes only its affected windows.
   Offroad-only partial review remains diagnostic rather than completed-trial
   proof. At least one independently closed original segment is required; an
   interrupted first/only segment cannot be recovered by this policy.

   Galaxy independently snapshots all route directories, including lock and log
   metadata that the drive library hides. An unavailable trailing suffix no
   longer causes the intact contiguous prefix to be rejected. Interior holes,
   changed sources and cancellation still fail. Source repair or newly acquired
   locks invalidate cached coverage; replacement never double-counts a route.
   One unreadable route cannot prevent the remaining dashboard scan.

   Parked permission and hardware checks remain required. Indoor USB power must
   still provide the device/Panda status expected by the managed worker; this
   patch does not treat missing physical status as permission to run. The change
   applies after installing this fork, not to an unchanged upstream Dom device.

10. **The Overview could hide analysis failures behind "Auto Learn is on".**
    Upstream's status selector checks the generic parked preference before its
    unavailable-report branch. A fresh recording/fit refusal therefore looks
    like idle enabled learning. An analysis heartbeat older than the freshness
    bound also falls back to the same label. The worker checks work roughly every
    10 seconds and can advance across routes; alternating these titles alone is
    not evidence of progress through different segments or of a successful tune.

    Fresh failures now show the actual reason ahead of old findings and the
    generic preference. Stale reports explicitly say the current state is
    unknown. Live parked fitting, finished fits and candidates awaiting fresh
    qualification have separate truthful states. Explicit OFF still wins.
    Partial reports now accept the backend's bounded 2,048-segment inventory,
    rather than an obsolete 180 limit. N/N coverage requires explicit absence of
    proven route completion; contradictory completion claims remain rejected.

11. **The comma-four installer can fail before this repository is downloaded.**
    The device's AGNOS 20.5.2 setup terminal reported that `/tmp/installer` could
    not load `libwayland-egl.so.1`. The hosted installer imports the older Wayland
    graphics backend. Its process exits before creating `/data/continue.sh`, so
    the AGNOS setup loop returns to software selection. The same failure affects
    upstream Dom; changing FLM does not repair that earlier loader stage.

    A fresh public shallow clone, checkout, Git object check, vendored-source
    validation and release-receipt validation all passed. The fork's hosted
    installer is byte-identical to comma's official comma-four installer except
    for its repository/branch placeholders. Its HTTP length and ELF fields are
    valid. No missing or fake graphics libraries should be added to AGNOS.

    `installer-dom` is a static AArch64 bootstrap built from the included
    `tools/installer` sources, with no ELF interpreter or shared-library needs.
    It runs a fixed fresh-install transaction: pin the public Dom commit, clone
    into its own staging folder, validate vendored source and the release receipt,
    install without replacing any existing target, and publish the AGNOS
    continuation last. The existing screen may appear unchanged during download;
    this bootstrap has no second graphical installer screen. Failures remain in
    `/data/starpilot-installer.log`. It does not modify AGNOS or graphics packages.
    Existing software must first be uninstalled through its normal UI.

## Verification

- Final affected Python regression suite: **1,075 passed**, with **269 passing subtests**.
  Existing tests emit one
  multiprocessing warning and three Cap'n Proto repeated-write warnings.
- Recovery fixtures run the actual isolated parked fitter after an indoor reboot,
  with locked, empty, truncated and missing-route-end inputs. Closed sections
  produce only a provisional candidate; original files, locks and accepted-tune
  Params remain unchanged. Tests also cover a wholly interrupted first segment,
  repair replacement, unknown-boundary buffers, bounded 14-of-355 selection and
  cancellation after a closed section. Galaxy tests cover hidden unavailable
  suffixes, newly locked sources, cache-version retries and per-route isolation.
- Additional merged-upstream regression suite: **669 passed**, with **471 passing
  subtests**, covering GM/Subaru callers and native handoffs, firmware, migration,
  radar association, model smoothing, speed control and update startup. The source
  copy needs its own declared dirty QA build metadata and source-built x86 vision
  IPC binding; these QA-only artifacts are not part of the device download.
- The latest upstream merge additionally passes **1,623 tests and 100 subtests**
  with two skips across Hyundai display ownership, Toyota intent/source gaps,
  regional settings migration, Home branch labels, browser setup and retained-OS
  update contracts. The 1,075-test FLM/Galaxy suite was repeated on this merge.
- Installer contracts: **24 tests passed**, Ruff and pinned ty passed, and a
  clean second ARM build was byte-identical. The 597,856-byte ELF has SHA-256
  `9163ed8a8820308800d5b7c811411a292addd51a7acfed08dfa5487853264404`.
  Its argument-refusal path also executed successfully on the actual comma four
  while leaving installation targets unchanged. This proves startup without the
  old Wayland dependency; it is separate from full installation and driving QA.
- **102 non-browser Galaxy JavaScript test files passed.** The patched dashboard
  also passed real headless Chrome checks at 416 and 1280 pixel widths: observed
  distance and partial-coverage labels render without overflow or browser errors.
  The separate upstream FLM browser harness assumes POSIX paths and did not run
  successfully on this Windows host; the dashboard browser checks use a local
  fixture and the actual patched Home component instead. Separate Chrome checks
  also exercise the actual new route-review and learning-mode components at both
  widths, including queuing a Connect review and saving Live mode through local
  API fixtures. No browser errors or horizontal overflow were observed.
- Actual FLM Overview Chrome checks at exact 416 and 1280 pixel widths exercise
  analyzing, fresh refusal, stale ongoing work and a queued candidate with earlier
  findings retained. The visible heading follows the current status, separately
  saved values remain available, and no current steering application is claimed.
- Regression coverage includes registered Sonata parked/live scope agreement,
  different devices/firmware/reset generations, missing identity refusal,
  355-segment admission and 513-segment refusal, damaged/interior/cancelled logs,
  durable partial statistics, replacement after repair, exclusions, HTTP access,
  controller replay, persistence and shutdown.
- Live-mode integration with a discovered VIN reaches the actual Controls
  application path, applies only the existing bounded correction, then withdraws
  it on driver override. Mode selection is parked-only, checks the vehicle/settings
  revision, and preserves explicit OFF and the original saved CarParams.
- The actual isolated parked fitter runs against a compressed Cereal fixture,
  preserves both saved-tune Params, skips repeated decoding of unchanged input,
  and is reaped on lost parked permission. Numerical fixtures verify independent
  parked fits, scope/profile/reset refusal, fresh native-evidence requirements,
  and live rollback without promoting the interrupted trial. Real message rows
  verify that driver pulses between joins and across a segment boundary preserve
  unrelated clean sections on both sides.
- Full FLM/Controls Ruff and lockfile-pinned **ty 0.0.80** checks pass. A newer ty
  version changes rule behavior; QA uses the repository's locked tool version.
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
in this repository. The driving runtime retains the upstream ARM binaries; the
separate installation bootstrap is newly compiled from its included sources.
This is not a new full driving-system build or a road-tested calibration. Device
authentication/download failure paths are tested with API fixtures; authenticated
Connect import and live performance still need validation on the actual comma.

## First validation after installation

1. Keep Auto Learn enabled for the selected vehicle, with no manually owned FLM
   surface and Safe Mode off. Choosing a learning mode does not turn Auto Learn on.
2. **Live + parked finishing · default** is selected automatically for supported
   vehicles without a saved mode choice. Under **Galaxy → Steering → Overview →
   When Auto Learn runs**, select **Offroad only · next-drive tune** while parked
   if preferred, then **Save for next drive**. Live learning remains experimental
   and needs validation on the actual vehicle.
3. Keeping power through ignition off allows normal logger closure. If power is
   lost first, powering the comma indoors can now analyze intact closed sections
   and finish a partial fit while parked. Leave it powered through analysis and
   ensure the parked hardware status is available. New-build controller metadata
   is required for the live handoff: an old recording cannot invent it. Galaxy
   reports the candidate or why more matching clean evidence is needed. Partial
   source coverage and statistical agreement alone never confirm a live trial;
   a bounded processing refusal leaves the accepted tune unchanged.
4. Existing local recordings are reviewed automatically while parked. For a
   particular cloud route, use **Steering → Activity → Existing routes** and
   explicitly request it. Dashboard statistics remain a separate analysis path;
   observed partial-drive statistics and historical reports are never substituted
   for completed tuning proof.
