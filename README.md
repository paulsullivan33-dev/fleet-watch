# fleet-watch

Mutual fleet monitor for a home AI lab. Two small always-on boxes
(e.g. an Arduino Uno Q and a Raspberry Pi 4) watch each other and the
rest of the fleet, and send [ntfy](https://ntfy.sh) push alerts when
something needs attention.

## How it works

Every run (cron, every 10 minutes):

1. **Health checks** over passwordless SSH: reachability, load vs cores,
   disk and inode usage, memory, OOM kills from the last 24h
   (`OOM_WINDOW_HOURS`), Ollama API, temperature, failed systemd
   services, pending-reboot flag, SMART disk health (best-effort).
2. **Stuck-job detection**: a duel/inference process alive plus its newest
   per-duel transcript not growing plus the inference server pegged =
   stuck (CRIT). A stale log with an idle server = probably loading the
   next model (WARN).
3. **Peer heartbeat**: each monitor checks the other's heartbeat timestamp.
   Two consecutive failures = the peer is silent (CRIT) — so if one box
   dies, the other tells you.
4. **Model triage, only when something looks off**: clean runs skip the
   model call entirely. When a check trips, a model on the always-on AI
   box (see Remote triage below) explains the problem in plain language
   and suggests a fix. If the triage endpoint is unreachable, the alert
   goes out with the rule-based findings instead — plus a WARN that the
   triage brain is offline.
5. **Disk advisor**: when a mount crosses the warn/crit threshold, the
   triage names what's eating the space (model blobs, logs, journals)
   and suggests concrete cleanup commands, cheapest first.

## Coexistence with duels

The health checks and heartbeats are plain SSH — no impact on running
duels. The triage model call only fires when something already looks
wrong. When triage runs on a remote box (`OLLAMA_HOST` pointing
elsewhere), nothing loads locally at all, so duels are completely
unaffected. With local triage the old OOM guard still applies: if a duel
is mid-turn with a *different* model in memory, the triage call is
skipped (loading two models at once OOM-killed small boxes before) and
the alert goes out with the rule-based findings instead.

## Remote triage

By default the triage model runs on the monitor itself (`OLLAMA_HOST`
defaults to `http://localhost:11434`). Point it at a bigger always-on
box for better verdicts and zero local memory pressure:

```bash
OLLAMA_HOST="http://192.168.1.236:11434"
MODEL="qwen3.5:4b"
```

The target's Ollama must listen on the LAN (`OLLAMA_HOST=0.0.0.0` in
its systemd environment — note: Ollama has no auth, so keep this
LAN-only). The monitor probes the endpoint every run and raises a WARN
if it's unreachable; triage then falls back to the rule-based verdicts.

## Alert discipline
- Alerts fire **only on state changes** — no repeat spam for the same
  ongoing issue (one reminder if it's still broken after 6h). Volatile
  measurements (ages, percentages, PID lists) are normalized out of the
  dedup hash so steady states stay quiet.
- All notification titles carry the sender hostname (`[name]`), so you
  can tell which monitor is talking at a glance.
- A "recovered" note goes out when things clear, so silence stays trustworthy.
- A low-priority daily "alive" ping proves the monitor itself is working.
- ntfy priorities: 3 = warning, 4 = critical, 2 = recovered, 1 = daily ping.

## Files

- `fleet-watch.sh` — the monitor (bash, zero non-standard deps beyond
  `ssh`, `curl`, `python3`, `ollama`)
- `triage-prompt.txt` — baselines, few-shot examples, and output format
  for the triage model
- `fleet-watch.conf.q` / `fleet-watch.conf.pi` — per-box config templates;
  copy the right one to `fleet-watch.conf` and edit the marked lines (peer
  host, fleet hosts, ntfy topic URL, `OLLAMA_HOST`/`MODEL` for remote triage)

## Install

```bash
# from a machine that can reach the box:
scp fleet-watch.sh fleet-watch.conf.q triage-prompt.txt user@box:~/fleet-watch/
ssh user@box 'cd ~/fleet-watch && mv fleet-watch.conf.q fleet-watch.conf \
  && chmod +x fleet-watch.sh && chmod 600 fleet-watch.conf'
```

Passwordless SSH is required in *both* directions, not just from your
workstation: your workstation -> each monitor (install, debug), **and**
each monitor -> the other monitor (the peer heartbeat check runs over
SSH and uses `BatchMode=yes`, so a password prompt means a failed check).

```bash
# if a monitor has no keypair yet:
ssh user@box1 'ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519'
# then exchange keys both ways (one-time password prompt each):
ssh user@box1 'ssh-copy-id user@box2'
ssh user@box2 'ssh-copy-id user@box1'
# sanity check (should print a number, not an error):
ssh user@box1 "ssh -o BatchMode=yes user@box2 'cat ~/.fleet-watch/heartbeat'"
```

Verify the copy, set `DRY_RUN=1` in the conf, and run once by hand — it
prints what it *would* send without touching ntfy. Then flip `DRY_RUN`
off and add to cron on both boxes:

```
*/10 * * * * /usr/bin/timeout 540 /home/user/fleet-watch/fleet-watch.sh
```

State lives in `~/.fleet-watch/` (log, state, heartbeat). The first real
run sends the daily "alive" ping so you know it's working.
