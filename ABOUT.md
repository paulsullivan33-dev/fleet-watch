# About fleet-watch

## Why it exists

A home lab with sixteen hosts breaks in quiet ways: a disk fills slowly
over weeks, a service dies after an update, a duel wedges at 2 AM and
burns CPU until morning. Checking everything by hand doesn't scale, and
commercial monitoring is overkill for a LAN. fleet-watch is the middle
path — a single bash script, run from cron, that watches the fleet and
only taps you on the shoulder when something actually needs attention.

## Why two monitors

The two smallest always-on boxes (an Arduino Uno Q and a Raspberry Pi 4)
watch each other as well as the fleet. Each one checks the other's
heartbeat every run, so there is no single point of failure: if a
monitor itself dies, the surviving one reports it. Either box can cover
the job alone; together they cover each other.

## Design principles

- **Quiet unless something is real.** Alerts fire on state changes only,
  with a six-hour reminder for still-broken issues and a "recovered" note
  when things clear. A daily low-priority ping proves the monitor is
  alive, so silence means "working," not "broken."
- **Rules detect, models judge.** Deterministic checks (disk, services,
  reachability) decide *whether* something is wrong; a small language
  model explains *what* it means in plain language and suggests a fix.
  The model only runs when a check already tripped — clean runs never
  pay for inference.
- **Don't disturb running work.** Health checks are plain SSH. Triage
  runs on a bigger always-on box over the LAN, so the little monitors
  never load a model locally and duels are never interrupted.
- **Boring technology.** Bash, ssh, curl, python3, ntfy. No agents, no
  daemons, no database — state is a few files under `~/.fleet-watch/`.

## Evolution

- Started with each monitor running a 1.7B model locally for triage.
  That worked until a duel plus a triage call OOM-killed the little
  boxes, so triage learned to stand down during duels.
- Triage moved to a dedicated always-on box on the LAN: better verdicts
  from a bigger model, zero memory pressure on the monitors, and the
  duel-time stand-down was no longer needed.
- The check list grew with real incidents: stuck-duel detection from a
  wedged batch, failed-service and reboot-required checks from a box
  that quietly degraded after an update, SMART health for aging spinning
  disks.
