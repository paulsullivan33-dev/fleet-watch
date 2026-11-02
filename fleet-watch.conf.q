# fleet-watch.conf for the ARDUINO Q.
# Copy to ~/fleet-watch/fleet-watch.conf on the Q and edit the marked lines.
# Keep this file chmod 600: it holds your ntfy URL.

# Host (as SSH understands it) of the OTHER monitor. Use the exact same
# string here and in FLEET_HOSTS on the other box if you list it there.
PEER_HOST="paul@192.168.1.109"

# Other fleet boxes to check (space-separated, as SSH understands them).
# Do NOT include PEER_HOST here; the peer is checked via heartbeat.
# EDIT to match your real hostnames:
FLEET_HOSTS="paul@paul-Vostro-260a paul@paul-OptiPlex-7010"

# Your ntfy topic URL. EDIT: e.g. https://ntfy.sh/my-fleet-topic
# or https://ntfy.example.com/my-fleet-topic
NTFY_URL="https://ntfy.sh/REPLACE_WITH_YOUR_TOPIC"

# Small model on THIS box used for triage.
MODEL="qwen3:1.7b"

# Where duel logs live on THIS box.
DUEL_DIR="$HOME/dual"

# Check cadence (should match the cron interval) and thresholds.
INTERVAL_MIN=10
DISK_WARN=85
DISK_CRIT=95
# A duel whose log hasn't grown in this long (minutes) is suspicious.
# Small models at 2-3 tok/s can take 20+ min per turn; 60 is conservative.
STUCK_AFTER_MIN=60
# Only OOM kills newer than this (hours) raise a flag; older dmesg
# entries are ignored so a single past event doesn't nag forever.
OOM_WINDOW_HOURS=24

OLLAMA_HOST="http://localhost:11434"

# Hosts where Ollama is expected, space-separated as SSH understands them
# ("localhost" means this box). Hosts NOT listed skip the Ollama API check
# entirely. EDIT: list your Ollama boxes, e.g.
# OLLAMA_HOSTS="localhost paul@192.168.1.171"
# Leave empty/unset to check Ollama on every host (old behavior, noisy if
# a fleet box like a sim server never runs Ollama).
#OLLAMA_HOSTS=""

# --- Expansion checks (2026-11-02). Empty list/string disables a check.
# fileList web UIs (healthy = ANY HTTP answer; 302 to login is normal),
# plus the job worker process on FILELIST_WORKER_HOST.
FILELIST_HOSTS="192.168.1.119 192.168.1.118 192.168.1.103 192.168.1.248"
FILELIST_PORT=8081
FILELIST_WORKER_HOST="paul@192.168.1.119"
# NerdMiners (drop-out alerts only for miners seen before).
MINER_IPS="192.168.1.125 192.168.1.154 192.168.1.188 192.168.1.190"
# Daily newly-failed systemd units. Hosts this box cannot SSH to are
# reported as coverage gaps, never alerts.
FAILED_UNIT_HOSTS="paul@192.168.1.236 paul@192.168.1.119 paul@192.168.1.118 paul@192.168.1.103 paul@192.168.1.248 paul@192.168.1.185 paul@192.168.1.165 paul@192.168.1.179"
FAILED_UNITS_INTERVAL_H=23
# usenetFetch stall watch (silent whenever usenetFetch is not running).
USENET_HOST="paul@192.168.1.119"
USENET_STALL_MIN=60

# Set to 1 for a test run: prints what WOULD be sent, sends nothing.
DRY_RUN=0
