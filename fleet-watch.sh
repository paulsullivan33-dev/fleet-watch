#!/usr/bin/env bash
#
# fleet-watch.sh — mutual fleet monitor for Paul's home lab.
#
# Runs on the Arduino Q and the Raspberry Pi 4 (one copy per box).
# Each run:
#   1. Checks local health + fleet health over SSH (load, disk, mem,
#      OOM kills, Ollama API, temperature).
#   2. Watches for stuck duels: duel process alive + run_results.log
#      not growing + llama-server pegged = stuck.
#   3. Checks the peer monitor's heartbeat (Q watches Pi, Pi watches Q).
#   4. If anything looks off, asks the local small model to triage and
#      explain; otherwise stays quiet (no model call on clean runs).
#   5. Sends ntfy alerts ONLY on state changes (no repeat spam),
#      plus a low-priority "recovered" note and a daily alive ping.
#
# Needs: bash, ssh (passwordless to fleet), curl, python3, ollama.
# Install: copy this + triage-prompt.txt + the right fleet-watch.conf
#          to ~/fleet-watch/ on each box, chmod +x, test with DRY_RUN=1,
#          then add to cron:
#   */10 * * * * /usr/bin/timeout 540 /home/paul/fleet-watch/fleet-watch.sh
#
set -u
cd "$(dirname "$0")" || exit 1

CONF="./fleet-watch.conf"
[ -f "$CONF" ] || { echo "fleet-watch: missing $CONF" >&2; exit 1; }
# shellcheck disable=SC1090
source "$CONF"

: "${PEER_HOST:?set PEER_HOST in fleet-watch.conf}"
: "${NTFY_URL:?set NTFY_URL in fleet-watch.conf}"
: "${MODEL:=qwen3:1.7b}"
: "${FLEET_HOSTS:=}"
: "${DUEL_DIR:=$HOME/dual}"
: "${INTERVAL_MIN:=10}"
: "${DISK_WARN:=85}"
: "${DISK_CRIT:=95}"
# Only OOM kills newer than this (hours) raise a flag. dmesg keeps old
# entries indefinitely, so without a window one past event nags forever.
: "${OOM_WINDOW_HOURS:=24}"
OOM_WINDOW_SEC=$(( OOM_WINDOW_HOURS * 3600 ))
: "${STUCK_AFTER_MIN:=60}"
: "${OLLAMA_HOST:=http://localhost:11434}"
: "${DRY_RUN:=0}"

STATE_DIR="$HOME/.fleet-watch"
LOG_FILE="$STATE_DIR/fleet-watch.log"
STATE_FILE="$STATE_DIR/state"
HEARTBEAT_FILE="$STATE_DIR/heartbeat"
REPORT_FILE="$STATE_DIR/report.txt"
PROMPT_FILE="./triage-prompt.txt"
mkdir -p "$STATE_DIR"

# Single-instance guard.
exec 9>"$STATE_DIR/lock"
flock -n 9 || { echo "fleet-watch: another run in progress, exiting"; exit 0; }

log() { echo "$(date '+%F %T') $*" >> "$LOG_FILE"; }

state_get() { grep -E "^$1=" "$STATE_FILE" 2>/dev/null | tail -1 | cut -d= -f2-; }
state_set() {
  local k=$1 v=$2 tmp
  tmp=$(mktemp)
  grep -vE "^$k=" "$STATE_FILE" 2>/dev/null > "$tmp" || true
  echo "$k=$v" >> "$tmp"
  mv "$tmp" "$STATE_FILE"
}

notify() { # $1=priority $2=title $3=message [$4=tags]
  local pri=$1 title=$2 msg=$3 tags=${4:-satellite}
  if [ "$DRY_RUN" = "1" ]; then
    echo "DRY-RUN ntfy pri=$pri [$title]: $(echo "$msg" | head -3 | tr '\n' '|')"
    return 0
  fi
  if curl -s -m 15 -H "Title: $title" -H "Priority: $pri" -H "Tags: $tags" \
       -d "$msg" "$NTFY_URL" >/dev/null; then
    log "ntfy sent (pri $pri): $title"
  else
    log "ntfy FAILED (pri $pri): $title"
  fi
}

FLAGS=()
REPORT=""

# One SSH call per host, parse locally.
PROBE='echo "== uptime"; uptime; echo "== df"; df -Ph / /home 2>/dev/null | head -8; echo "== mem"; free -m | head -2; echo "== cores"; nproc; echo "== temp"; (vcgencmd measure_temp 2>/dev/null || awk "{print \$1/1000 \" C\"}" /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo n/a); echo "== oom"; up=$(cut -d. -f1 /proc/uptime 2>/dev/null || echo 0); dmesg 2>/dev/null | grep -i "killed process" | awk -v up="$up" -v win='"$OOM_WINDOW_SEC"' -F"[][]" "{t=\$2+0; if (up-t<win) print}" | tail -3; echo "== ollama"; curl -s -m 5 http://localhost:11434/api/tags -o /dev/null -w "api_http=%{http_code}\n" || echo "api=down"'

DEEP_DISK='echo "== du"; timeout 50 du -sh ~/.ollama/models ~/dual/logs 2>/dev/null; echo "== topdirs"; timeout 50 du -sh ~/* 2>/dev/null | sort -rh | head -6; echo "== journal"; journalctl --disk-usage 2>/dev/null | head -2; echo "== models"; timeout 20 ollama list 2>/dev/null | head -12'

run_probe() { # $1=host ("localhost" = local)
  if [ "$1" = "localhost" ]; then
    bash -c "$PROBE" 2>&1
  else
    ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new "$1" "$PROBE" 2>&1
  fi
}

run_deep_disk() { # $1=host
  if [ "$1" = "localhost" ]; then
    bash -c "$DEEP_DISK" 2>&1
  else
    ssh -o BatchMode=yes -o ConnectTimeout=15 "$1" "$DEEP_DISK" 2>&1
  fi
}

disk_flags_for() { # $1=label $2=probe-output
  local label=$1 out=$2 usage mnt
  while read -r usage mnt; do
    [ -z "$usage" ] && continue
    if [ "$usage" -ge "$DISK_CRIT" ]; then
      FLAGS+=("crit:disk:$label $mnt at ${usage}%")
    elif [ "$usage" -ge "$DISK_WARN" ]; then
      FLAGS+=("warn:disk:$label $mnt at ${usage}%")
    fi
  done < <(echo "$out" | awk '/^\/dev\// {u=$5; gsub(/%/,"",u); if (u+0>0) print u, $6}')
}

check_host() { # $1=host $2=label
  local host=$1 label=$2 out rc=0
  out=$(run_probe "$host") || rc=$?
  if [ "$rc" -ne 0 ] || [ -z "$out" ]; then
    REPORT+="[$label] UNREACHABLE"$'\n'
    FLAGS+=("crit:unreachable:$label did not answer SSH")
    return
  fi
  REPORT+="[$label]"$'\n'"$out"$'\n'
  disk_flags_for "$label" "$out"
  if echo "$out" | grep -qi "killed process"; then
    FLAGS+=("warn:oom:$label shows OOM-killed processes in dmesg (last ${OOM_WINDOW_HOURS}h)")
  fi
  if echo "$out" | grep -q "api=down"; then
    FLAGS+=("warn:ollama_down:$label Ollama API not answering")
  fi
  # Deep disk detail only when a threshold tripped (keeps clean runs fast).
  if echo "$out" | awk '/^\/dev\// {u=$5; gsub(/%/,"",u); if (u+0>=0) print u}' \
       | awk -v w="$DISK_WARN" '$1>=w{found=1} END{exit !found}'; then
    REPORT+="[disk detail $label]"$'\n'"$(run_deep_disk "$host")"$'\n'
  fi
}

check_duels() { # local box only
  local pids now mtime age_m maxcpu=0 p scpu spids log
  pids=$(pgrep -f "[o]llama_duel.py" || true)
  if [ -z "$pids" ]; then REPORT+="[duels] none running"$'\n'; return 0; fi
  now=$(date +%s)
  REPORT+="[duels] pids: $pids"$'\n'
  log="$DUEL_DIR/run_results.log"
  age_m=0
  if [ -f "$log" ]; then
    mtime=$(stat -c %Y "$log")
    age_m=$(( (now - mtime) / 60 ))
    REPORT+="[duels] run_results.log age: ${age_m}m"$'\n'
  else
    REPORT+="[duels] no run_results.log in $DUEL_DIR"$'\n'
  fi
  spids=$(pgrep -f "[l]lama-server" || true)
  for p in $spids; do
    scpu=$(ps -o %cpu= -p "$p" 2>/dev/null | awk '{print int($1+0)}')
    [ "${scpu:-0}" -gt "$maxcpu" ] && maxcpu=$scpu
  done
  REPORT+="[duels] llama-server max cpu: ${maxcpu}%"$'\n'
  if [ "$age_m" -gt "$STUCK_AFTER_MIN" ] && [ "$maxcpu" -gt 70 ]; then
    FLAGS+=("crit:duel_stuck:run_results.log ${age_m}m old while llama-server pegged at ${maxcpu}% (pids $pids)")
  elif [ "$age_m" -gt "$STUCK_AFTER_MIN" ]; then
    FLAGS+=("warn:duel_idle:run_results.log ${age_m}m old, llama-server idle (probably loading next model)")
  fi
}

peer_check() {
  local rts now age fails
  fails=$(state_get peer_fail_count); fails=${fails:-0}
  if rts=$(ssh -o BatchMode=yes -o ConnectTimeout=8 "$PEER_HOST" 'cat ~/.fleet-watch/heartbeat' 2>/dev/null) \
     && [ -n "$rts" ]; then
    now=$(date +%s); age=$(( (now - rts) / 60 ))
    REPORT+="[peer $PEER_HOST] heartbeat age: ${age}m"$'\n'
    [ "$age" -gt $(( INTERVAL_MIN * 3 )) ] \
      && FLAGS+=("warn:peer_stale:$PEER_HOST heartbeat ${age}m old")
    state_set peer_fail_count 0
  else
    fails=$((fails + 1)); state_set peer_fail_count "$fails"
    REPORT+="[peer $PEER_HOST] heartbeat unreachable (consecutive fail $fails)"$'\n'
    [ "$fails" -ge 2 ] && FLAGS+=("crit:peer_silent:$PEER_HOST silent for 2 consecutive checks")
  fi
}

call_model() { # reads $REPORT_FILE, prints model response
  python3 - "$PROMPT_FILE" "$REPORT_FILE" "$MODEL" "$OLLAMA_HOST" <<'PYEOF' 2>/dev/null
import json, sys, urllib.request
prompt_path, report_path, model, host = sys.argv[1:5]
prompt = open(prompt_path).read()
report = open(report_path).read()
body = json.dumps({
    "model": model,
    "prompt": prompt + "\n\n# CURRENT FLEET REPORT (times America/Chicago)\n" + report,
    "stream": False,
}).encode()
req = urllib.request.Request(host + "/api/generate", data=body,
                             headers={"Content-Type": "application/json"})
print(json.load(urllib.request.urlopen(req, timeout=300))["response"])
PYEOF
}

worst_sev() { # prints OK/WARN/CRIT
  local s="OK" f sev
  if [ "${#FLAGS[@]}" -gt 0 ]; then
    for f in "${FLAGS[@]}"; do
      sev=${f%%:*}
      [ "$sev" = "warn" ] && s="WARN"
      [ "$sev" = "crit" ] && s="CRIT"
    done
  fi
  echo "$s"
}

rule_verdict() { # fallback when the model is unavailable
  local f rest
  for f in "${FLAGS[@]}"; do rest=${f#*:}; echo "- $rest"; done
}

maybe_alert() { # $1=sev $2=reason $3=full text
  local sev=$1 reason=$2 full=$3 hash now last_hash last_time
  hash=$(printf '%s' "$sev|$reason" | md5sum | cut -d' ' -f1)
  now=$(date +%s)
  last_hash=$(state_get last_alert_hash); last_hash=${last_hash:-none}
  last_time=$(state_get last_alert_time); last_time=${last_time:-0}
  if [ "$hash" = "$last_hash" ] && [ $(( now - last_time )) -lt 21600 ]; then
    log "alert suppressed (same issue <6h): $reason"
    return 0
  fi
  local pri=3 tags="warning"
  if [ "$sev" = "CRIT" ]; then pri=4; tags="rotating_light"; fi
  notify "$pri" "fleet-watch $sev" "$full" "$tags"
  state_set last_alert_hash "$hash"
  state_set last_alert_time "$now"
  state_set last_verdict "$sev"
}

# ---------------- main ----------------
REPORT="# fleet-watch run $(date '+%F %T %Z')"$'\n'

check_host "localhost" "self($(hostname))"
for h in $FLEET_HOSTS; do
  # Peer is checked separately via heartbeat; don't double-cover it here.
  case " $PEER_HOST " in *" $h "*) continue;; esac
  check_host "$h" "$h"
done
check_duels
peer_check

echo "$REPORT" > "$REPORT_FILE"

SEV=$(worst_sev)
LAST=$(state_get last_verdict); LAST=${LAST:-OK}

if [ "$SEV" = "OK" ]; then
  if [ "$LAST" != "OK" ]; then
    notify 2 "fleet-watch recovered" "All checks clear again. Previous state: $LAST." "white_check_mark"
    log "recovered (was $LAST)"
  fi
  # Daily alive ping so silence means "working", not "broken".
  now=$(date +%s); lp=$(state_get last_daily_ping); lp=${lp:-0}
  if [ $(( now - lp )) -gt 86400 ]; then
    notify 1 "fleet-watch alive" "Daily check-in: all fleet checks clear." "zzz"
    state_set last_daily_ping "$now"
  fi
  state_set last_verdict "OK"
  log "OK"
else
  # OOM safety: if a duel is mid-turn with a *different* model loaded,
  # don't ask Ollama to load the triage model too — two 1.7B models in
  # memory OOM-killed these boxes before. Fall back to the rule-based
  # verdict instead (the VERDICT parse below handles the empty answer).
  ANSWER=""
  if ! pgrep -f "[o]llama_duel.py" >/dev/null 2>&1 \
     || ollama ps 2>/dev/null | grep -q "$MODEL"; then
    ANSWER=$(call_model)
  else
    log "triage model call skipped: duel active with another model loaded"
  fi
  VERDICT=$(echo "$ANSWER" | grep -i -m1 "^VERDICT:" || true)
  if [ -z "$VERDICT" ]; then
    # Model unavailable or misbehaved: fall back to the raw flags.
    VERDICT="VERDICT: $SEV: (model triage unavailable) $(rule_verdict | tr '\n' ';')"
    FULL="fleet-watch $SEV (model triage unavailable — raw findings):"$'\n'"$(rule_verdict)"
  else
    FULL="fleet-watch triage ($SEV):"$'\n'"$ANSWER"
  fi
  REASON=$(echo "$VERDICT" | sed -E 's/^VERDICT:[[:space:]]*//I' | cut -c1-160)
  maybe_alert "$SEV" "$REASON" "$FULL"
  log "$SEV: $REASON"
fi

date +%s > "$HEARTBEAT_FILE"
