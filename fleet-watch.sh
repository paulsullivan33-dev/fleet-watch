#!/usr/bin/env bash
#
# fleet-watch.sh — mutual fleet monitor for Paul's home lab.
#
# Runs on the Arduino Q and the Raspberry Pi 4 (one copy per box).
# Each run:
#   1. Checks local health + fleet health over SSH (load, disk, mem,
#      OOM kills, Ollama API, temperature).
#   2. Watches duels: alerts when a duel starts/ends (with the scenario
#      name), and flags stuck duels: duel process alive + no duel output
#      (freshest of run_results.log and the per-duel logs/*.log transcripts,
#      since run_results.log only updates when a duel completes) for
#      STUCK_AFTER_MIN minutes + llama-server pegged = stuck.
#   3. Checks the peer monitor's heartbeat (Q watches Pi, Pi watches Q).
#   4. If anything looks off, asks the local small model to triage and
#      explain; otherwise stays quiet (no model call on clean runs).
#   5. Sends ntfy alerts ONLY on state changes (no repeat spam),
#      plus a low-priority "recovered" note and a daily alive ping.
#
# Every run appends to ~/.fleet-watch/fleet-watch.log: the exact probe
# commands issued, the full results, and the complete triage-model
# response (rotated to the last $LOG_KEEP_LINES lines), so the log shows
# what "normal" looks like.
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
# Lines of history kept in fleet-watch.log (oldest trimmed once per run).
: "${LOG_KEEP_LINES:=50000}"
# Hosts where Ollama is expected, space-separated as SSH understands them
# ("localhost" means this box). Hosts NOT listed skip the Ollama API check
# entirely: no ollama_down flag, no alert. Empty (default) keeps the old
# behavior of checking Ollama on every host.
: "${OLLAMA_HOSTS:=}"

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

log_block() { # $1=tag; logs stdin as a delimited block
  {
    echo "===== $(date '+%F %T') [$1] begin ====="
    cat
    echo "===== $(date '+%F %T') [$1] end ====="
  } >> "$LOG_FILE"
}

rotate_log() { # keep the log from growing without bound
  [ -f "$LOG_FILE" ] || return 0
  local lines
  lines=$(wc -l < "$LOG_FILE")
  if [ "$lines" -gt "$LOG_KEEP_LINES" ]; then
    tail -n "$LOG_KEEP_LINES" "$LOG_FILE" > "$LOG_FILE.tmp" \
      && mv "$LOG_FILE.tmp" "$LOG_FILE"
  fi
}

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
    log "DRY-RUN would send ntfy (pri $pri): $title"
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
PROBE='echo "== uptime"; uptime; echo "== df"; df -Ph -x tmpfs -x devtmpfs -x overlay -x squashfs 2>/dev/null; echo "== mem"; free -m | head -2; echo "== cores"; nproc; echo "== temp"; (vcgencmd measure_temp 2>/dev/null || awk "{print \$1/1000 \" C\"}" /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo n/a); echo "== oom"; up=$(cut -d. -f1 /proc/uptime 2>/dev/null || echo 0); dmesg 2>/dev/null | grep -i "killed process" | awk -v up="$up" -v win='"$OOM_WINDOW_SEC"' -F"[][]" "{t=\$2+0; if (up-t<win) print}" | tail -3; echo "== ollama"; curl -s -m 5 http://localhost:11434/api/tags -o /dev/null -w "api_http=%{http_code}\n" || echo "api=down"'

DEEP_DISK='echo "== du"; timeout 50 du -sh ~/.ollama/models ~/dual/logs 2>/dev/null; echo "== topdirs"; timeout 50 du -sh ~/* 2>/dev/null | sort -rh | head -6; echo "== journal"; journalctl --disk-usage 2>/dev/null | head -2; echo "== models"; timeout 20 ollama list 2>/dev/null | head -12'

run_probe() { # $1=host ("localhost" = local)
  if [ "$1" = "localhost" ]; then
    log "probe -> localhost (bash -c PROBE)"
    bash -c "$PROBE" 2>&1
  else
    log "probe -> $1 (ssh PROBE)"
    ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new "$1" "$PROBE" 2>&1
  fi
}

run_deep_disk() { # $1=host [$2=command override, defaults to $DEEP_DISK]
  local cmd=${2:-$DEEP_DISK}
  log "deep-disk -> $1 (ssh DEEP_DISK)"
  if [ "$1" = "localhost" ]; then
    bash -c "$cmd" 2>&1
  else
    ssh -o BatchMode=yes -o ConnectTimeout=15 "$1" "$cmd" 2>&1
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

triage_host_line() { # $1=label $2=probe-output -> compact one-liner for the model
  local label=$1 out=$2 up disk mem oll
  up=$(printf '%s' "$out" | awk '/^== uptime$/{getline; print; exit}')
  up=${up#* }  # strip leading clock time
  disk=$(printf '%s' "$out" | awk '/^\/dev\// {u=$5; gsub(/%/,"",u); if (u+0>max+0) {max=u; mnt=$6}} END {if (max>0) print max"% ("mnt")"}')
  mem=$(printf '%s' "$out" | awk '$1=="Mem:" {print $7"M avail"}')
  oll=$(printf '%s' "$out" | grep -o 'api_http=[0-9]*\|api=down' | head -1)
  printf -- '- %s: %s | worst disk: %s | mem: %s | ollama: %s' \
    "$label" "${up:-n/a}" "${disk:-n/a}" "${mem:-n/a}" "${oll:-n/a}"
}

ollama_expected() { # $1=host as passed to check_host
  # Empty OLLAMA_HOSTS keeps the legacy behavior: check every host.
  [ -z "$OLLAMA_HOSTS" ] && return 0
  case " $OLLAMA_HOSTS " in *" $1 "*) return 0;; *) return 1;; esac
}

check_host() { # $1=host $2=label
  local host=$1 label=$2 out rc=0 detail tripped deep_cmd mnt
  out=$(run_probe "$host") || rc=$?
  if [ "$rc" -ne 0 ] || [ -z "$out" ]; then
    REPORT+="[$label] UNREACHABLE"$'\n'
    TRIAGE_SUMMARY+="- $label: UNREACHABLE via SSH"$'\n'
    FLAGS+=("crit:unreachable:$label did not answer SSH")
    return
  fi
  REPORT+="[$label]"$'\n'"$out"$'\n'
  TRIAGE_SUMMARY+="$(triage_host_line "$label" "$out")"$'\n'
  disk_flags_for "$label" "$out"
  if echo "$out" | grep -qi "killed process"; then
    FLAGS+=("warn:oom:$label shows OOM-killed processes in dmesg (last ${OOM_WINDOW_HOURS}h)")
  fi
  if echo "$out" | grep -q "api=down"; then
    if ollama_expected "$host"; then
      FLAGS+=("warn:ollama_down:$label Ollama API not answering")
    else
      REPORT+="(Ollama check skipped for $label: not in OLLAMA_HOSTS)"$'\n'
    fi
  fi
  # Deep disk detail only when a threshold tripped (keeps clean runs fast).
  # Target the tripped mount(s) specifically: the base probe only covers
  # $HOME, so without this the model gets home-dir details for a warning
  # about a different disk and writes about the wrong directories.
  tripped=$(echo "$out" | awk -v w="$DISK_WARN" '/^\/dev\// {u=$5; gsub(/%/,"",u); if (u+0>=w) print $6}')
  if [ -n "$tripped" ]; then
    deep_cmd=$DEEP_DISK
    for mnt in $tripped; do
      deep_cmd="$deep_cmd; echo \"== topdirs $mnt\"; timeout 60 du -sh \"$mnt\"/* 2>/dev/null | sort -rh | head -8"
    done
    detail=$(run_deep_disk "$host" "$deep_cmd")
    REPORT+="[disk detail $label]"$'\n'"$detail"$'\n'
    TRIAGE_DETAIL+="--- disk detail: $label"$'\n'"$detail"$'\n'
  fi
}

duel_name() { # $1=pid -> best-effort scenario name from the command line
  # ollama_duel.py takes the scenario JSON path as its first positional arg,
  # e.g. "python3 ollama_duel.py scenarios/mac_roast_battle.json --turns 8".
  local cmdline cfg name
  cmdline=$(ps -o args= -p "$1" 2>/dev/null || true)
  cfg=$(printf '%s' "$cmdline" | sed -E 's/.*ollama_duel\.py[[:space:]]+([^[:space:]]+).*/\1/')
  if [ "$cfg" != "$cmdline" ] && [ -n "$cfg" ]; then
    name=$(basename "$cfg" .json | tr ' ' '_')
  else
    name="pid-$1"
  fi
  printf '%s' "$name"
}

track_duels() { # $1=pids (space-separated, may be empty); alerts on start/end
  local p cur="" prev pair entry started="" ended=""
  for p in $1; do
    cur+="$p:$(duel_name "$p") "
  done
  cur=$(printf '%s' "$cur" | tr -s ' ' | sed -e 's/^ *//' -e 's/ *$//')
  prev=$(state_get duel_pids); prev=${prev:-}
  [ "$cur" = "$prev" ] && return 0
  for pair in $cur; do
    case " $prev " in *" $pair "*) ;; *) started+="$pair ";; esac
  done
  for pair in $prev; do
    case " $cur " in *" $pair "*) ;; *) ended+="$pair ";; esac
  done
  for pair in $started; do
    entry=${pair#*:}
    notify 2 "fleet-watch duel started" "Duel started on $(hostname): $entry."
    log "duel started: $entry"
  done
  for pair in $ended; do
    entry=${pair#*:}
    notify 2 "fleet-watch duel ended" "Duel ended on $(hostname): $entry."
    log "duel ended: $entry"
  done
  state_set duel_pids "$cur"
  REPORT+="[duels] tracked: ${cur:-none} (was: ${prev:-none})"$'\n'
}

check_duels() { # local box only
  local pids now mtime age_m maxcpu=0 p scpu spids log newest newest_mtime newest_age_m
  pids=$(pgrep -f "[o]llama_duel.py" || true)
  track_duels "$pids"
  if [ -z "$pids" ]; then REPORT+="[duels] none running"$'\n'; return 0; fi
  now=$(date +%s)
  REPORT+="[duels] pids: $pids"$'\n'
  log="$DUEL_DIR/run_results.log"
  age_m=
  if [ -f "$log" ]; then
    mtime=$(stat -c %Y "$log")
    age_m=$(( (now - mtime) / 60 ))
    REPORT+="[duels] run_results.log age: ${age_m}m"$'\n'
  else
    REPORT+="[duels] no run_results.log in $DUEL_DIR"$'\n'
  fi
  # Per-duel transcripts (output/logs/*.log) grow while a duel is working, but
  # run_results.log only updates when a duel completes — so a long duel
  # looked "stuck" even while generating. Use the freshest of the two as
  # the liveness signal, falling back to run_results.log alone.
  if [ -d "$DUEL_DIR/output/logs" ]; then
    newest=$(ls -t "$DUEL_DIR"/output/logs/*.log 2>/dev/null | head -n 1)
    if [ -n "$newest" ]; then
      newest_mtime=$(stat -c %Y "$newest")
      newest_age_m=$(( (now - newest_mtime) / 60 ))
      REPORT+="[duels] newest transcript: $(basename "$newest") (${newest_age_m}m old)"$'\n'
      if [ -z "${age_m:-}" ] || [ "$newest_age_m" -lt "$age_m" ]; then
        age_m=$newest_age_m
      fi
    fi
  fi
  age_m=${age_m:-0}
  spids=$(pgrep -f "[l]lama-server" || true)
  for p in $spids; do
    scpu=$(ps -o %cpu= -p "$p" 2>/dev/null | awk '{print int($1+0)}')
    [ "${scpu:-0}" -gt "$maxcpu" ] && maxcpu=$scpu
  done
  REPORT+="[duels] llama-server max cpu: ${maxcpu}%"$'\n'
  if [ "$age_m" -gt "$STUCK_AFTER_MIN" ] && [ "$maxcpu" -gt 70 ]; then
    FLAGS+=("crit:duel_stuck:no duel output for ${age_m}m while llama-server pegged at ${maxcpu}% (pids $pids)")
  elif [ "$age_m" -gt "$STUCK_AFTER_MIN" ]; then
    FLAGS+=("warn:duel_idle:no duel output for ${age_m}m, llama-server idle (probably loading next model)")
  fi
}

peer_check() {
  local rts now age fails
  fails=$(state_get peer_fail_count); fails=${fails:-0}
  if rts=$(ssh -o BatchMode=yes -o ConnectTimeout=8 "$PEER_HOST" 'cat ~/.fleet-watch/heartbeat' 2>/dev/null) \
     && [ -n "$rts" ]; then
    now=$(date +%s); age=$(( (now - rts) / 60 ))
    REPORT+="[peer $PEER_HOST] heartbeat age: ${age}m"$'\n'
    log "peer $PEER_HOST: heartbeat age ${age}m"
    [ "$age" -gt $(( INTERVAL_MIN * 3 )) ] \
      && FLAGS+=("warn:peer_stale:$PEER_HOST heartbeat ${age}m old")
    state_set peer_fail_count 0
  else
    fails=$((fails + 1)); state_set peer_fail_count "$fails"
    REPORT+="[peer $PEER_HOST] heartbeat unreachable (consecutive fail $fails)"$'\n'
    log "peer $PEER_HOST: heartbeat unreachable (consecutive fail $fails)"
    [ "$fails" -ge 2 ] && FLAGS+=("crit:peer_silent:$PEER_HOST silent for 2 consecutive checks")
  fi
}

call_model() { # reads $TRIAGE_FILE (compact), prints model response
  python3 - "$PROMPT_FILE" "$TRIAGE_FILE" "$MODEL" "$OLLAMA_HOST" <<'PYEOF' 2>/dev/null
import json, sys, urllib.request
prompt_path, report_path, model, host = sys.argv[1:5]
prompt = open(prompt_path).read()
report = open(report_path).read()
body = json.dumps({
    "model": model,
    "prompt": prompt + "\n\n# CURRENT FLEET SUMMARY (times America/Chicago)\n" + report,
    "stream": False,
    "think": False,  # qwen3 thinking trace is pure overhead for a 2-sentence verdict; on slow ARM boxes it blows the 300s timeout
    "options": {"num_predict": 150},  # hard-cap output: 2 sentences + VERDICT + suggestions fits easily; stops rambling/repetition from eating the timeout
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

maybe_alert() { # $1=sev $2=reason $3=full text [$4=dedup key]
  # Dedup key defaults to sev|reason, but callers with non-deterministic
  # reason text (model verdicts) must pass the rule flags instead --
  # otherwise every run hashes differently and the <6h suppression
  # never fires.
  local sev=$1 reason=$2 full=$3 key=${4:-$sev|$reason} hash now last_hash last_time
  hash=$(printf '%s' "$key" | md5sum | cut -d' ' -f1)
  now=$(date +%s)
  last_hash=$(state_get last_alert_hash); last_hash=${last_hash:-none}
  last_time=$(state_get last_alert_time); last_time=${last_time:-0}
  if [ "$hash" = "$last_hash" ] && [ $(( now - last_time )) -lt 21600 ]; then
    log "alert suppressed (same issue <6h): $reason"
    return 0
  fi
  local pri=3 tags="warning"
  if [ "$sev" = "CRIT" ]; then pri=4; tags="rotating_light"; fi
  notify "$pri" "fleet-watch $sev [$(hostname)]" "$full" "$tags"
  state_set last_alert_hash "$hash"
  state_set last_alert_time "$now"
  state_set last_verdict "$sev"
}

# ---------------- main ----------------
rotate_log
log "run start (DRY_RUN=$DRY_RUN)"
# The exact commands issued this run (PROBE/DEEP_DISK are constant, so log
# their text once; per-host invocations are logged in run_probe/run_deep_disk).
printf '%s\n' "$PROBE" | log_block "command PROBE"
printf '%s\n' "$DEEP_DISK" | log_block "command DEEP_DISK"
REPORT="# fleet-watch run $(date '+%F %T %Z')"$'\n'
# Compact triage input (built alongside REPORT): the full report is too many
# tokens to prefill inside the 300s model timeout on ARM (~2.6 tok/s), so the
# model gets flags + one-line host summaries + disk detail instead.
TRIAGE_SUMMARY=""
TRIAGE_DETAIL=""

check_host "localhost" "self($(hostname))"
for h in $FLEET_HOSTS; do
  # Peer is checked separately via heartbeat; don't double-cover it here.
  case " $PEER_HOST " in *" $h "*) continue;; esac
  check_host "$h" "$h"
done
check_duels
peer_check

echo "$REPORT" > "$REPORT_FILE"
# Full results in the log: this is the "what was checked" record.
printf '%s\n' "$REPORT" | log_block "report"

# Compact model input, written every run (cheap); the triage call below reads
# it instead of the full report. Also logged so the record shows exactly what
# the model saw.
TRIAGE_FILE="$STATE_DIR/triage_input.txt"
{
  echo "# FLAGS (what fired this run)"
  rule_verdict
  echo "# HOST SUMMARIES (one line each)"
  printf '%s' "$TRIAGE_SUMMARY"
  if [ -n "$TRIAGE_DETAIL" ]; then
    echo "# DISK DETAIL (only hosts that tripped a disk threshold)"
    printf '%s' "$TRIAGE_DETAIL"
  fi
} > "$TRIAGE_FILE"
printf '%s\n' "$(cat "$TRIAGE_FILE")" | log_block "triage input"

SEV=$(worst_sev)
LAST=$(state_get last_verdict); LAST=${LAST:-OK}

if [ "$SEV" = "OK" ]; then
  if [ "$LAST" != "OK" ]; then
    notify 2 "fleet-watch recovered [$(hostname)]" "All checks clear again. Previous state: $LAST." "white_check_mark"
    log "recovered (was $LAST)"
  fi
  # Daily alive ping so silence means "working", not "broken".
  now=$(date +%s); lp=$(state_get last_daily_ping); lp=${lp:-0}
  if [ $(( now - lp )) -gt 86400 ]; then
    notify 1 "fleet-watch alive [$(hostname)]" "Daily check-in: all fleet checks clear." "zzz"
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
    log "triage: calling $MODEL at $OLLAMA_HOST"
    ANSWER=$(call_model)
    printf '%s\n' "$ANSWER" | log_block "triage response ($MODEL)"
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
  # Dedup on the rule flags (deterministic), not the model prose.
  maybe_alert "$SEV" "$REASON" "$FULL" "$SEV|$(rule_verdict | tr '\n' ';')"
  log "$SEV: $REASON"
fi

log "run end: $SEV"
date +%s > "$HEARTBEAT_FILE"
