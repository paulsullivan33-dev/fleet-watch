# FleetWatch alert expansion — build report, 2026-11-02

Status: **built and tested, NOT deployed, NOT pushed, nothing committed.**
Working tree of `~/workspace/fleet-watch/` (verified at origin/main
`3f0006e` before starting; `git fetch` showed 0 commits behind).

Changes: `fleet-watch.sh` (+~350 lines), `fleet-watch.conf.q`,
`fleet-watch.conf.pi`, `README.md`, new `tests/test_expansion.py`.
All four checks are config-driven (empty list/string disables), follow
the existing patterns (probe/eval split, `state_get`/`state_set`,
2-consecutive-miss `unreach_*` debounce, FLAGS → split triage →
`maybe_alert` dedup), support `DRY_RUN=1` like everything else, and add
read-only hints in `hint_for` for the new flag kinds
(`filelist`, `miner_down`, `failed_new`, `usenet_stall`).

Tests: **32/32 green** (`python3 -m unittest discover -s tests`) —
13 pre-existing + 19 new in `tests/test_expansion.py`.

## 1. fileList health — `check_filelist`

- Web: `GET http://<ip>:8081/` for `FILELIST_HOSTS`
  (192.168.1.119/.118/.103/.248). Healthy = any HTTP status (302 to the
  login page is the app's normal answer, verified live from the Q for
  all four). "000" (nothing answered) debounces over 2 consecutive
  checks before flagging, same as host-unreachable.
- Worker (.119 only, `FILELIST_WORKER_HOST=paul@192.168.1.119`): SSH
  probe `pgrep -f "[j]ob_worker.py"`. Investigation findings: run.sh
  supervises `./start`, which runs `.venv/bin/python job_worker.py`
  beside `app.py`; the worker holds an flock on
  `~/.local/state/filelist/jobs/worker.lock` for its whole life (the
  lock file's mtime is static, so it's not a heartbeat — process
  presence is the reliable read-only signal, and it was verified live:
  PID 46861). Worker-down debounces over 2 checks, then flags
  "queued jobs will sit stranded" — the 2026-11-01/02 failure mode.
- Gap (per spec, accepted for v1): .118/.103/.248 get the web check
  only; their workers aren't probed. Extending it later is one config
  change per host if Paul wants it (worker host is a single setting
  today — multi-host worker checks would need a small refactor).
- If the worker SSH probe itself fails, the run logs "worker state
  unknown" — a note, never a flag.

## 2. NerdMiner drop-off — `check_miners`

- Every run: GET `http://<ip>/swarm` for `MINER_IPS`
  (.125/.154/.188/.190); present set = answering IPs ∪ union of all
  `devices[].ip` lists (one answering miner names the whole swarm —
  verified live from the Q: .125's answer listed all four).
- State: `miners_seen` (ever observed) and `miners_down`. A seen miner
  absent for 2+ consecutive checks flags `warn:miner_down` once;
  further absences don't re-flag (state + the existing 6h dedup), and
  its return is logged and noted in the report ("back in the swarm"),
  which also produces the normal fleet-watch "recovered" ntfy when it
  was the only open issue. A never-seen miner is reported as
  "never seen — not alerting" and can never flag.

## 3. Failed systemd units, daily — `check_failed_units`

- Gated by `failed_units_last_run` state + `FAILED_UNITS_INTERVAL_H`
  (default 23h, so a skipped day self-heals). Probes
  `systemctl --failed --no-legend` over SSH on `FAILED_UNIT_HOSTS`
  (.236/.119/.118/.103/.248/.185/.165/.179), parses unit names with the
  same bullet-strip approach as the existing `failed_svc_info`, and
  diffs against a stored per-host set: only **newly** failed units
  flag `warn:failed_new`. First successful probe of a host records a
  silent baseline (no alert storm for pre-existing failures); a unit
  that recovers and re-fails alerts again.
- **SSH access — tested live from BOTH watchers (paul account,
  BatchMode), 2026-11-02:**
  - ✅ Reachable: **.119, .118, .248** (the current FLEET_HOSTS).
  - ❌ **No access: .236 (ProDesk), .103 (mx2), .185 (corvus),
    .165 (deb), .179 (minikube)** — from both the Q and the Pi.
  Per instructions, no keys or authorized_keys were touched. In the
  code these five are probed each daily run, fail fast, and land in the
  report as coverage gaps (never flags). They start working the moment
  the watchers' public keys are authorized there — Paul's call.
- Note: on .119/.118/.248 this overlaps the existing 10-minute
  failed-services flag (same units, coarser text). A *new* failure
  there can produce both alerts. Paul may want .119/.118/.248 dropped
  from `FAILED_UNIT_HOSTS` to keep the daily check purely for hosts
  outside FLEET_HOSTS — left as-is (all 8) per the approved spec;
  one-line config change either way.

## 4. usenetFetch stall — `check_usenet_stall`

- Probe interface = two SSH calls to `USENET_HOST`
  (paul@192.168.1.119 — reachable from both watchers, so no local probe
  on .119 was needed; the "tiny local probe" fallback is **not**
  required). The split is deliberate: the process call contains
  "usenet-fetch" only as the bracketed pgrep pattern `[u]senet-fetch`,
  so the remote shell running the probe can't match itself; the DB
  call (whose text names `usenet-fetch.db`) runs separately.
- Sample per run: process running? (pgrep), cumulative CPU seconds
  (`ps -o times=` summed over matching PIDs), `mtime` = max(DB, DB-WAL)
  — the DB is in WAL mode and the WAL held the freshest write in live
  testing (13:32 vs the main file's 10:23) — and pending count =
  `SELECT COUNT(*) FROM pending_releases` via
  `sqlite3 "file:…?mode=ro"` (the queue table the tool drains;
  verified live read: **16,918**, matching the count measured earlier
  that day).
- Rule (`usenet_eval`): not running → clear streak, stay silent
  (normal state — confirmed live: no usenet-fetch process was running
  during this build, and the dry run stayed silent). Running with
  pending = 0 or count unreadable → silent. Otherwise the
  `pending|cpu|mtime` sample is stored; an identical sample older than
  `USENET_STALL_MIN` (default 60) minutes flags `warn:usenet_stall`.
  Any movement in any one signal resets the streak — slow-but-alive
  can't trip it.

## Dry-run evidence

Full-script `DRY_RUN=1` run from a scratch dir on the Pi (real watcher
vantage; scratch HOME, dummy ntfy topic — nothing sent, deployed
copies untouched, scratch deleted afterwards). Exit 0. The only alert
was a pre-existing check (share1 disk at exactly 85%). The new checks'
report lines:

```
[filelist 192.168.1.119] web answering (HTTP 302)
[filelist 192.168.1.119] job worker running
[filelist 192.168.1.118] web answering (HTTP 302)
[filelist 192.168.1.103] web answering (HTTP 302)
[filelist 192.168.1.248] web answering (HTTP 302)
[miners] present:192.168.1.125 192.168.1.154 192.168.1.188 192.168.1.190 | down: none
[failed-units paul@192.168.1.236] SSH FAILED — cannot check (coverage gap)
[failed-units paul@192.168.1.119] baseline recorded (failed now: none — pre-existing, not alerting)
[failed-units paul@192.168.1.118] baseline recorded (failed now: none — pre-existing, not alerting)
[failed-units paul@192.168.1.103] SSH FAILED — cannot check (coverage gap)
[failed-units paul@192.168.1.248] baseline recorded (failed now: none — pre-existing, not alerting)
[failed-units paul@192.168.1.185] SSH FAILED — cannot check (coverage gap)
[failed-units paul@192.168.1.165] SSH FAILED — cannot check (coverage gap)
[failed-units paul@192.168.1.179] SSH FAILED — cannot check (coverage gap)
[usenet] usenetFetch not running on paul@192.168.1.119 (normal — stall check silent)
```

Alert-path sample output (from the unit tests, which drive the eval
functions with canned probes — a healthy fleet can't produce these
live on demand):

- fileList web down (2nd miss): `warn:filelist:192.168.1.119 fileList web UI not answering on http://192.168.1.119:8081/ (2+ consecutive checks)`
- fileList worker down (2nd miss): `warn:filelist:192.168.1.119 fileList job worker (job_worker.py) not running — queued jobs will sit stranded`
- Miner out (2nd miss): `warn:miner_down:192.168.1.154 NerdMiner has dropped out of the swarm (absent 2+ consecutive checks)`
- New failed unit: `warn:failed_new:paul@192.168.1.185 newly failed systemd unit(s): b.service`
- usenetFetch stalled (frozen 60m): `warn:usenet_stall:paul@192.168.1.119 usenetFetch appears stalled: process running, 16918 releases pending, but no DB writes, no CPU progress and no queue change for 60m`
  (frozen 30m → no flag; any signal moving → streak resets; both
  covered by tests.)

Side note from the dry run: /myData/share1 on the Vostro sat at exactly
85% (the warn threshold) at run time — the existing disk check, not
part of this build, but Paul may see that WARN from the real monitors.

## Needs Paul's decision

1. **Deploy** (separately authorized as always): copy the updated
   `fleet-watch.sh` to Q + Pi. The deployed confs predate the new keys
   and don't need edits — script defaults match the fleet (the dry run
   above ran with a conf that sets none of the new keys). Updating the
   deployed confs from the refreshed templates is optional hygiene.
2. **Watcher SSH coverage**: authorize the Q/Pi paul keys on
   .236/.103/.185/.165/.179 if he wants failed-unit coverage there, or
   trim `FAILED_UNIT_HOSTS`. Not done — keys untouched per instructions.
3. **Overlap**: keep .119/.118/.248 in the daily newly-failed check
   (double-alert possible alongside the 10-minute failedsvc flag) or
   trim them (see check 3 note).
4. **fileList worker checks for .118/.103/.248**: v1 is web-only there;
   say the word if he wants worker probes on those too.
5. Commit/push of this working tree, when he's ready to review it.

Nothing was deployed, pushed, committed, or restarted. The ProDesk was
only read (Ollama `/api/tags` GETs and the triage call the dry run's
pre-existing disk WARN triggered, same as any normal monitor run);
the running coop rejudge was not disturbed.
