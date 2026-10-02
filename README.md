# fleet-watch

Mutual fleet monitor for a home AI lab. Two small always-on boxes
(e.g. an Arduino Uno Q and a Raspberry Pi 4) watch each other and the
rest of the fleet, and send [ntfy](https://ntfy.sh) push alerts when
something needs attention.

## How it works

Every run (cron, every 10 minutes):

1. **Health checks** over passwordless SSH: reachability, load vs cores,
   disk, memory, OOM kills from the last 24h (`OOM_WINDOW_HOURS`),
   Ollama API, temperature.
2. **Stuck-job detection**: a duel/inference process alive plus its progress
   log not growing plus the inference server pegged = stuck (CRIT).
   A stale log with an idle server = probably loading the next model (WARN).
3. **Peer heartbeat**: each monitor checks the other's heartbeat timestamp.
   Two consecutive failures = the peer is silent (CRIT) — so if one box
   dies, the other tells you.
4. **Small-model triage, only when something looks off**: clean runs skip
   the model call entirely. When a check trips, the local small model
   (1.7B-class is fine) explains the problem in plain language.
5. **Disk advisor**: when a mount crosses the warn/crit threshold, the
   triage names what's eating the space (model blobs, logs, journals)
   and suggests concrete cleanup commands, cheapest first.

## Coexistence with duels

The health checks and heartbeats are plain SSH — no impact on running
duels. The triage model call only fires when something already looks
wrong, and it reuses the already-loaded model when it can. If a duel is
mid-turn with a *different* model in memory, the triage call is skipped
entirely (loading two models at once OOM-killed small boxes before) and
the alert goes out with the rule-based findings instead.

## Alert discipline
- Alerts fire **only on state changes** — no repeat spam for the same
  ongoing issue (one reminder if it's still broken after 6h).
- A "recovered" note goes out when things clear, so silence stays trustworthy.
- A low-priority daily "alive" ping proves the monitor itself is working.
- ntfy priorities: 3 = warning, 4 = critical, 2 = recovered, 1 = daily ping.

## Files

- `fleet-watch.sh` — the monitor (bash, zero non-standard deps beyond
  `ssh`, `curl`, `python3`, `ollama`)
- `triage-prompt.txt` — baselines, few-shot examples, and output format
  for the triage model
- `fleet-watch.conf.q` / `fleet-watch.conf.pi` — per-box configs; copy the
  right one to `fleet-watch.conf` and edit the marked lines (peer host,
  fleet hosts, ntfy topic URL)

## Install

```bash
# from a machine that can reach the box:
scp fleet-watch.sh fleet-watch.conf.q triage-prompt.txt user@box:~/fleet-watch/
ssh user@box 'cd ~/fleet-watch && mv fleet-watch.conf.q fleet-watch.conf \
  && chmod +x fleet-watch.sh && chmod 600 fleet-watch.conf'
```

Verify the copy, set `DRY_RUN=1` in the conf, and run once by hand — it
prints what it *would* send without touching ntfy. Then flip `DRY_RUN`
off and add to cron on both boxes:

```
*/10 * * * * /usr/bin/timeout 540 /home/user/fleet-watch/fleet-watch.sh
```

State lives in `~/.fleet-watch/` (log, state, heartbeat). The first real
run sends the daily "alive" ping so you know it's working.
