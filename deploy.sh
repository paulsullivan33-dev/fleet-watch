#!/usr/bin/env bash
# deploy.sh -- deploy fleet_watch.py to the two watchers (Pi .109, Q .171).
#
# Replaces copyThem, which belonged to the retired bash watcher and
# pushed fleet-watch.sh. This script deploys the Python watcher:
#   fleet_watch.py    -> ~/fleet-watch/fleet_watch.py    (both watchers)
#   triage-prompt.txt -> ~/fleet-watch/triage-prompt.txt (both watchers)
#
# The per-watcher config (~/fleet-watch/fleet-watch.conf) is NOT
# deployed: fleet-watch.conf.pi / fleet-watch.conf.q in this repo are
# generic templates, while the live configs hold site values (ntfy
# topic, host lists) that do not belong in a public repo. The live
# configs are managed on the watchers themselves; this script only
# checks that each one is present. (Live copies are backed up on the
# Vostro under ~/backups/fleet-watch-conf/.)
#
# Usage:
#   ./deploy.sh           check only: report MATCH/DIFF per file, change nothing
#   ./deploy.sh --apply   copy the files, then re-run the check
#
# Run it from a host on the fleet LAN (e.g. the Vostro) with ssh access
# to the watchers. It never touches the watchers' cron lines -- the
# existing entries already run fleet_watch.py in place.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PI="paul@192.168.1.109"
Q="paul@192.168.1.171"
REMOTE_DIR="~/fleet-watch"
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

rc=0
check_one(){ # host localfile remotename
  local host="$1" lf="$2" rn="$3" lh rh
  lh=$(sha256sum "$HERE/$lf" | cut -d' ' -f1)
  rh=$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" "sha256sum $REMOTE_DIR/$rn 2>/dev/null" | cut -d' ' -f1)
  if [ -n "$rh" ] && [ "$lh" = "$rh" ]; then
    echo "MATCH  $host  $rn"
  else
    echo "DIFF   $host  $rn (local ${lh:-?} vs remote ${rh:-missing})"
    rc=1
  fi
}
check_conf(){ # host -- presence only; confs are watcher-local (see header)
  if ssh -o BatchMode=yes -o ConnectTimeout=10 "$1" "test -s $REMOTE_DIR/fleet-watch.conf"; then
    echo "PRESENT $1  fleet-watch.conf (watcher-local, not deployed)"
  else
    echo "MISSING $1  fleet-watch.conf"
    rc=1
  fi
}
check_all(){
  rc=0
  check_one "$PI" fleet_watch.py fleet_watch.py
  check_one "$Q"  fleet_watch.py fleet_watch.py
  check_one "$PI" triage-prompt.txt triage-prompt.txt
  check_one "$Q"  triage-prompt.txt triage-prompt.txt
  check_conf "$PI"
  check_conf "$Q"
  return $rc
}
push_one(){ # localfile host remotename
  scp -o BatchMode=yes -o ConnectTimeout=10 "$HERE/$1" "$2:$REMOTE_DIR/$3" \
    && echo "copied $1 -> $2 $3" || { echo "FAILED copying $1 -> $2"; rc=1; }
}

if [ "$APPLY" = 1 ]; then
  push_one fleet_watch.py "$PI" fleet_watch.py
  push_one fleet_watch.py "$Q"  fleet_watch.py
  push_one triage-prompt.txt "$PI" triage-prompt.txt
  push_one triage-prompt.txt "$Q"  triage-prompt.txt
  echo "--- verify after copy"
fi
check_all
exit $rc
