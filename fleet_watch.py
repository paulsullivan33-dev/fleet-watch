#!/usr/bin/env python3
"""fleet_watch.py — Python port of fleet-watch.sh (mutual fleet monitor).

Runs on the Arduino Q and the Raspberry Pi 4 (one copy per box), from the
same ~/fleet-watch/ directory, reading the same fleet-watch.conf and
triage-prompt.txt as the bash version. Behavior mirrors the bash watcher:

  1. Local + fleet health over SSH (load, disk, mem, OOM kills, Ollama
     API, temperature, failed services, SMART, read-only mounts, clock).
  2. Duel watching: start/end alerts, stuck-duel detection.
  3. Peer monitor heartbeat (Q watches Pi, Pi watches Q).
  4. Expansion checks: fileList web + job worker, NerdMiner drop-outs,
     daily newly-failed systemd units, usenetFetch stall.
  5. Split triage: one small Ollama call per issue group; this program
     assembles the alert, owns severity and suggested checks; the model
     only writes prose. Clean runs make no model call.
  6. ntfy alerts only on state changes (6h same-issue suppression),
     plus a "recovered" note and a daily alive ping.

Differences from the bash version, by design:
  * OOM kills are remembered per host (newest reported kernel timestamp
    + boot identity in state), so one already-reported kill never flags
    again; only a NEWER kill re-flags. The bash version re-derived the
    flag from dmesg on every run for the whole 24h window.
  * --beta mode (or FW_BETA=1): everything is labeled BETA — ntfy titles
    get a "BETA " prefix — and state/log/report live in
    ~/.fleet-watch-beta, so a beta watcher can shadow the production
    bash watcher on the same box without touching its state. The peer
    heartbeat is still READ from the production path (that is the point
    of the check); beta writes its own heartbeat only inside its own
    state dir, where nothing production reads it.

Needs: python3 (stdlib only), ssh, curl on PATH.
Install: copy alongside fleet-watch.sh; test with DRY_RUN=1, then e.g.
  3-53/10 * * * * /usr/bin/timeout 540 /usr/bin/python3 /home/paul/fleet-watch/fleet_watch.py --beta
"""

import fcntl
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- config

DEFAULTS = {
    "MODEL": "qwen3:1.7b",
    "FLEET_HOSTS": "",
    "DUEL_DIR": os.path.expanduser("~/dual"),
    "INTERVAL_MIN": "10",
    "DISK_WARN": "85",
    "DISK_CRIT": "95",
    "TEMP_WARN": "80",
    "OOM_WINDOW_HOURS": "24",
    "STUCK_AFTER_MIN": "60",
    "OLLAMA_HOST": "http://localhost:11434",
    "DRY_RUN": "0",
    "LOG_KEEP_LINES": "50000",
    "OLLAMA_HOSTS": "",
    "FILELIST_HOSTS": "192.168.1.119 192.168.1.118 192.168.1.103 192.168.1.248",
    "FILELIST_PORT": "8081",
    "FILELIST_WORKER_HOST": "paul@192.168.1.119",
    "MINER_IPS": "192.168.1.125 192.168.1.154 192.168.1.188 192.168.1.190",
    "FAILED_UNIT_HOSTS": "",
    "FAILED_UNITS_INTERVAL_H": "23",
    "USENET_HOST": "paul@192.168.1.119",
    "USENET_STALL_MIN": "60",
}


def load_config(path):
    """Parse a bash-style KEY=value conf. Conf wins over environment for
    keys it sets (same net effect as `source` + := defaults in bash);
    environment fills keys the conf does not set; DEFAULTS fill the rest.
    $HOME / ${HOME} and previously parsed $VARS expand in values."""
    cfg = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                cfg[key] = val
    known = set(DEFAULTS) | {"PEER_HOST", "NTFY_URL"} | set(cfg)
    merged = dict(DEFAULTS)
    for k in known:
        if k in os.environ and k not in cfg:
            merged[k] = os.environ[k]
    merged.update(cfg)
    # Expand $VARS (notably $HOME in DUEL_DIR) using environ + parsed cfg.
    for _ in range(3):
        changed = False
        for k, v in list(merged.items()):
            if "$" not in v:
                continue
            env = dict(os.environ)
            env.update(merged)
            nv = os.path.expandvars(v) if False else _expand(v, env)
            if nv != v:
                merged[k] = nv
                changed = True
        if not changed:
            break
    return merged


def _expand(val, env):
    def repl(m):
        name = m.group(1) or m.group(2)
        return env.get(name, m.group(0))
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", repl, val)


def cfg_int(cfg, key):
    try:
        return int(str(cfg.get(key, DEFAULTS.get(key, "0"))).strip())
    except ValueError:
        return 0


# ----------------------------------------------------------------- state

def state_key_for(label):
    return re.sub(r"[^A-Za-z0-9]", "_", str(label))


class State:
    """key=value text file, format-compatible with the bash state file."""

    def __init__(self, path):
        self.path = path
        self.data = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if "=" in line:
                        k, _, v = line.partition("=")
                        self.data[k] = v

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = str(value)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for k, v in self.data.items():
                f.write(f"{k}={v}\n")
        os.replace(tmp, self.path)


# ---------------------------------------------------------------- watcher

class Watcher:
    def __init__(self, beta=False, conf_path=None, home=None):
        self.beta = beta
        self.home = home or os.path.expanduser("~")
        conf_path = conf_path or os.path.join(SCRIPT_DIR, "fleet-watch.conf")
        self.cfg = load_config(conf_path)
        self.dry_run = str(self.cfg.get("DRY_RUN", "0")) == "1" or \
            os.environ.get("FW_DRY_RUN") == "1"
        suffix = "-beta" if beta else ""
        self.state_dir = os.path.join(self.home, f".fleet-watch{suffix}")
        os.makedirs(self.state_dir, exist_ok=True)
        self.log_file = os.path.join(self.state_dir, "fleet-watch.log")
        self.state = State(os.path.join(self.state_dir, "state"))
        self.heartbeat_file = os.path.join(self.state_dir, "heartbeat")
        self.report_file = os.path.join(self.state_dir, "report.txt")
        self.prompt_file = os.path.join(SCRIPT_DIR, "triage-prompt.txt")
        self.hostname = os.uname().nodename
        self.self_label = f"self({self.hostname})"
        self.flags = []            # "sev:kind:finding text"
        self.report = []           # report lines
        self.triage_summary = []   # one-liner per host/check
        self.detail_by_host = {}   # label -> deep-disk text
        self.oom_window_sec = cfg_int(self.cfg, "OOM_WINDOW_HOURS") * 3600

    # -- logging ------------------------------------------------------
    def log(self, msg):
        stamp = datetime.now().strftime("%F %T")
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(f"{stamp} {msg}\n")

    def log_block(self, tag, text):
        stamp = datetime.now().strftime("%F %T")
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(f"===== {stamp} [{tag}] begin =====\n")
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")
            f.write(f"===== {stamp} [{tag}] end =====\n")

    def rotate_log(self):
        keep = cfg_int(self.cfg, "LOG_KEEP_LINES")
        if keep <= 0 or not os.path.exists(self.log_file):
            return
        with open(self.log_file, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if len(lines) > keep:
            with open(self.log_file, "w", encoding="utf-8") as f:
                f.writelines(lines[-keep:])

    # -- notify -------------------------------------------------------
    def notify(self, pri, title, msg, tags="satellite"):
        if self.beta:
            title = "BETA " + title
        if self.dry_run:
            head = " | ".join(msg.splitlines()[:3])
            print(f"DRY-RUN ntfy pri={pri} [{title}]: {head}")
            self.log(f"DRY-RUN would send ntfy (pri {pri}): {title}")
            return True
        url = self.cfg.get("NTFY_URL", "")
        r = subprocess.run(
            ["curl", "-fsS", "-m", "15", "-H", f"Title: {title}",
             "-H", f"Priority: {pri}", "-H", f"Tags: {tags}",
             "-d", msg, url],
            capture_output=True, text=True)
        if r.returncode == 0:
            self.log(f"ntfy sent (pri {pri}): {title}")
            return True
        self.log(f"ntfy FAILED (pri {pri}): {title}")
        return False

    # -- unreachable debounce ------------------------------------------
    def unreach_should_flag(self, label):
        key = "unreach_" + state_key_for(label)
        n = int(self.state.get(key, "0") or 0) + 1
        self.state.set(key, n)
        return n >= 2

    def unreach_clear(self, label):
        self.state.set("unreach_" + state_key_for(label), 0)

    # -- shell runner ---------------------------------------------------
    @staticmethod
    def run(cmd, timeout=60):
        """Run a command list, merging stderr into stdout (bash 2>&1).
        Returns (rc, text). rc=124-style failures surface as rc!=0."""
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, stderr=subprocess.STDOUT)
            return r.returncode, r.stdout or ""
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            return 124, out
        except OSError as e:
            return 127, str(e)


# ----------------------------------------------------------------- probes
# The remote probe text mirrors the bash PROBE byte-for-byte except the
# OOM section: it now reports uptime seconds plus the last 5 kills
# UNfiltered, so the watcher can apply its per-event memory locally.

PROBE_TEMPLATE = r'''echo "== uptime"; uptime; echo "== df"; df -Ph -x tmpfs -x devtmpfs -x overlay -x squashfs 2>/dev/null; echo "== mem"; free -m | head -2; echo "== cores"; nproc; echo "== temp"; (vcgencmd measure_temp 2>/dev/null || awk "{print \$1/1000 \" C\"}" /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo n/a); echo "== oom"; up=$(cut -d. -f1 /proc/uptime 2>/dev/null || echo 0); echo "uptime_sec=$up"; dmesg 2>/dev/null | grep -i "killed process" | tail -5; echo "== ollama"; curl -s -m 5 http://localhost:11434/api/tags -o /dev/null -w "api_http=%{http_code}\n" || echo "api=down"; echo "== failedsvc"; systemctl --failed --no-legend 2>/dev/null | head -20; echo "== rebootreq"; [ -f /var/run/reboot-required ] && echo yes || echo no; echo "== inodes"; df -iP / 2>/dev/null | tail -1 | tr -s " " | cut -d" " -f5 | tr -cd "0-9"; echo "== smart"; for d in /dev/sda /dev/sdb /dev/nvme0n1 /dev/mmcblk0; do [ -b "$d" ] || continue; r=$(sudo -n smartctl -H "$d" 2>/dev/null || smartctl -H "$d" 2>/dev/null) || continue; w=$(echo "$r" | grep -oiE "passed|failed" | head -1); [ -n "$w" ] && echo "$d: $w"; done; echo "== ro"; grep -E " (ext4|xfs|btrfs) " /proc/mounts 2>/dev/null | grep -v "bind" | grep -vE " /(snap|var/snap)(/|$)" | cut -d" " -f2,4 | grep -E "[ ,]ro(,|$)" | cut -d" " -f1 | head -5; echo "== dmesg"; dmesg 2>/dev/null | grep -iE "I/O error|EXT4-fs error|Buffer I/O error|scsi error" | tail -5; echo "== clock"; timedatectl show -p NTPSynchronized --value 2>/dev/null || echo unknown; echo "== end"'''

DEEP_DISK = r'''echo "== du"; timeout 50 du -sh ~/.ollama/models ~/dual/logs 2>/dev/null; echo "== topdirs"; timeout 50 du -sh ~/* 2>/dev/null | sort -rh | head -6; echo "== journal"; journalctl --disk-usage 2>/dev/null | head -2; echo "== models"; timeout 20 ollama list 2>/dev/null | head -12'''

USENET_PROBE_PCPU = r'''pids=$(pgrep -f "[u]senet-fetch" || true); if [ -z "$pids" ]; then echo running=0; else echo running=1; c=0; for p in $pids; do t=$(ps -o times= -p "$p" 2>/dev/null | tr -d " "); c=$((c + ${t:-0})); done; echo "cpu=$c"; fi'''

USENET_PROBE_DB = r'''d="$HOME/usenetFetch"; m1=$(stat -c %Y "$d/usenet-fetch.db" 2>/dev/null || echo 0); m2=$(stat -c %Y "$d/usenet-fetch.db-wal" 2>/dev/null || echo 0); if [ "$m2" -gt "$m1" ]; then m=$m2; else m=$m1; fi; echo "mtime=$m"; p=$(timeout 60 sqlite3 "file:$d/usenet-fetch.db?mode=ro" "SELECT COUNT(*) FROM pending_releases;" 2>/dev/null || echo -1); echo "pending=$p"'''


def split_sections(out):
    """Split probe output into {section_name: [lines]} on '== name' lines."""
    sections = {}
    cur = None
    for line in out.splitlines():
        m = re.match(r"^== (.+)$", line)
        if m:
            cur = m.group(1)
            sections.setdefault(cur, [])
        elif cur is not None:
            sections[cur].append(line)
    return sections


# ------------------------------------------------------------ host checks

def run_probe(w, host):
    if host == "localhost":
        w.log("probe -> localhost (bash -c PROBE)")
        return w.run(["bash", "-c", PROBE_TEMPLATE], timeout=120)
    w.log(f"probe -> {host} (ssh PROBE)")
    return w.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                  "-o", "StrictHostKeyChecking=accept-new", host,
                  PROBE_TEMPLATE], timeout=120)


def run_deep_disk(w, host, cmd=None):
    cmd = cmd or DEEP_DISK
    w.log(f"deep-disk -> {host} (ssh DEEP_DISK)")
    if host == "localhost":
        return w.run(["bash", "-c", cmd], timeout=180)[1]
    return w.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                  host, cmd], timeout=180)[1]


def df_entries(out):
    """(usage_int, mount) for each /dev/ df line in the probe output."""
    entries = []
    for line in split_sections(out).get("df", []):
        if not line.startswith("/dev/"):
            continue
        parts = line.split()
        if len(parts) >= 6 and parts[4].endswith("%"):
            try:
                entries.append((int(parts[4].rstrip("%")), parts[5]))
            except ValueError:
                pass
    return entries


def disk_flags_for(w, label, out):
    for usage, mnt in df_entries(out):
        if usage >= cfg_int(w.cfg, "DISK_CRIT"):
            w.flags.append(f"crit:disk:{label} {mnt} at {usage}%")
        elif usage >= cfg_int(w.cfg, "DISK_WARN"):
            w.flags.append(f"warn:disk:{label} {mnt} at {usage}%")


def failed_svc_info(out):
    """(count, 'name1,name2') for the failedsvc section."""
    lines = [ln for ln in split_sections(out).get("failedsvc", []) if ln.strip()]
    names = []
    for ln in lines:
        ln = re.sub(r"^[^A-Za-z0-9_@.:-]+", "", ln)
        tok = ln.split()
        if tok:
            names.append(tok[0])
    return len(lines), ",".join(names[:5])


def _section_first(sections, name):
    lines = sections.get(name, [])
    return lines[0] if lines else ""


def triage_host_line(w, label, out):
    sections = split_sections(out)
    up = _section_first(sections, "uptime")
    up = up.split(" ", 1)[1] if " " in up else up  # strip leading clock time
    entries = df_entries(out)
    disk = ""
    if entries:
        usage, mnt = max(entries)
        disk = f"{usage}% ({mnt})"
    mem = ""
    for ln in sections.get("mem", []):
        p = ln.split()
        if p and p[0] == "Mem:" and len(p) >= 7:
            mem = f"{p[6]}M avail"
    oll = ""
    m = re.search(r"api_http=[0-9]+|api=down", out)
    if m:
        oll = m.group(0)
    extra = ""
    n, names = failed_svc_info(out)
    if n > 0:
        extra += f" | failed_svc:{n}({names})"
    if _section_first(sections, "rebootreq") == "yes":
        extra += " | reboot-required"
    ro = [ln for ln in sections.get("ro", []) if ln.strip()][:5]
    if ro:
        extra += " | readonly:" + ",".join(ro)
    dm_n = len([ln for ln in sections.get("dmesg", []) if ln.strip()])
    if dm_n > 0:
        extra += f" | dmesg_err:{dm_n}"
    if _section_first(sections, "clock") == "no":
        extra += " | clock:unsynced"
    tp = _section_first(sections, "temp")
    mnum = re.search(r"[0-9]+(\.[0-9]+)?", tp)
    if mnum and int(float(mnum.group(0))) >= cfg_int(w.cfg, "TEMP_WARN"):
        extra += f" | temp:{mnum.group(0)}C"
    return (f"- {label}: {up or 'n/a'} | worst disk: {disk or 'n/a'} | "
            f"mem: {mem or 'n/a'} | ollama: {oll or 'n/a'}{extra}")


def oom_check(w, label, out, now):
    """Per-event OOM memory: flag only kills NEWER than the newest one
    already reported for this host+boot. State: oom_seen_<label> =
    '<boot_epoch>:<max_kernel_ts>'. Kernel timestamps reset at reboot;
    a changed boot identity invalidates the stored timestamp (dmesg is
    fresh after a reboot anyway, so in-window kills are genuinely new).
    """
    sections = split_sections(out)
    up = 0
    kills = []
    for ln in sections.get("oom", []):
        if ln.startswith("uptime_sec="):
            try:
                up = int(ln.split("=", 1)[1])
            except ValueError:
                pass
        m = re.match(r"\[\s*(\d+)\.", ln)
        if m and "killed process" in ln.lower():
            kills.append(int(m.group(1)))
    if up <= 0 or not kills:
        return
    in_window = [t for t in kills if up - t < w.oom_window_sec]
    if not in_window:
        return
    boot_epoch = now - up
    key = "oom_seen_" + state_key_for(label)
    seen_ts = -1
    stored = w.state.get(key)
    if stored and ":" in stored:
        b, _, ts = stored.partition(":")
        try:
            if abs(int(b) - boot_epoch) <= 180:
                seen_ts = int(ts)
        except ValueError:
            pass
    if any(t > seen_ts for t in in_window):
        w.flags.append(
            f"warn:oom:{label} shows OOM-killed processes in dmesg "
            f"(last {cfg_int(w.cfg, 'OOM_WINDOW_HOURS')}h)")
    newest = max(in_window)
    if newest > seen_ts:
        w.state.set(key, f"{boot_epoch}:{newest}")


def ollama_expected(w, host):
    hosts = w.cfg.get("OLLAMA_HOSTS", "")
    if not hosts:
        return True
    return host in hosts.split()


def check_host(w, host, label, now=None):
    now = now or int(time.time())
    rc, out = run_probe(w, host)
    if rc != 0 or not out:
        w.report.append(f"[{label}] UNREACHABLE")
        if w.unreach_should_flag(label):
            w.triage_summary.append(
                f"- {label}: UNREACHABLE via SSH (2+ consecutive runs)")
            w.flags.append(
                f"crit:unreachable:{label} did not answer SSH "
                "(2+ consecutive runs)")
        else:
            w.triage_summary.append(
                f"- {label}: UNREACHABLE via SSH (first miss — alerting "
                "starts at 2 consecutive)")
            w.log(f"{label} unreachable: first consecutive miss (debounced)")
        return
    w.unreach_clear(label)
    w.report.append(f"[{label}]")
    w.report.extend(out.rstrip("\n").splitlines())
    w.triage_summary.append(triage_host_line(w, label, out))
    disk_flags_for(w, label, out)
    oom_check(w, label, out, now)
    sections = split_sections(out)
    if "api=down" in out:
        if ollama_expected(w, host):
            w.flags.append(f"warn:ollama_down:{label} Ollama API not answering")
        else:
            w.report.append(
                f"(Ollama check skipped for {label}: not in OLLAMA_HOSTS)")
    n, names = failed_svc_info(out)
    if n > 0:
        w.flags.append(
            f"warn:failedsvc:{label} has {n} failed systemd service(s): {names}")
    if _section_first(sections, "rebootreq") == "yes":
        w.flags.append(f"warn:reboot_required:{label} has a pending reboot")
    inodes_raw = _section_first(sections, "inodes").strip()
    inodes = int(inodes_raw) if inodes_raw.isdigit() else 0
    if inodes >= cfg_int(w.cfg, "DISK_CRIT"):
        w.flags.append(f"crit:inodes:{label} / at {inodes}% inodes used")
    elif inodes >= cfg_int(w.cfg, "DISK_WARN"):
        w.flags.append(f"warn:inodes:{label} / at {inodes}% inodes used")
    smart_bad = [ln for ln in sections.get("smart", [])
                 if "failed" in ln.lower()]
    if smart_bad:
        w.flags.append(
            f"warn:smart:{label} disk health FAILED: "
            + ";".join(smart_bad))
    ro_mounts = [ln for ln in sections.get("ro", []) if ln.strip()][:5]
    if ro_mounts:
        w.flags.append(
            f"warn:readonly:{label} read-only mount(s): " + ",".join(ro_mounts))
    dmesg_err = len([ln for ln in sections.get("dmesg", []) if ln.strip()])
    if dmesg_err > 0:
        w.flags.append(
            f"warn:dmesg:{label} {dmesg_err} disk/filesystem error(s) in dmesg")
    if _section_first(sections, "clock") == "no":
        w.flags.append(f"warn:clock:{label} system clock not NTP-synchronized")
    tp = _section_first(sections, "temp")
    mnum = re.search(r"[0-9]+(\.[0-9]+)?", tp)
    if mnum and int(float(mnum.group(0))) >= cfg_int(w.cfg, "TEMP_WARN"):
        w.flags.append(
            f"warn:temp:{label} CPU temp at or above "
            f"{cfg_int(w.cfg, 'TEMP_WARN')}C")
    tripped = [mnt for usage, mnt in df_entries(out)
               if usage >= cfg_int(w.cfg, "DISK_WARN")]
    if tripped:
        deep_cmd = DEEP_DISK
        for mnt in tripped:
            deep_cmd += (f'; echo "== topdirs {mnt}"; timeout 60 du -sh '
                         f'"{mnt}"/* 2>/dev/null | sort -rh | head -8')
        detail = run_deep_disk(w, host, deep_cmd)
        w.report.append(f"[disk detail {label}]")
        w.report.extend(detail.rstrip("\n").splitlines())
        w.detail_by_host[label] = detail


# ----------------------------------------------------------------- duels

def _pgrep(pattern):
    r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    return [p for p in r.stdout.split() if p.isdigit()]


def duel_name(pid):
    r = subprocess.run(["ps", "-o", "args=", "-p", str(pid)],
                       capture_output=True, text=True)
    cmdline = (r.stdout or "").strip()
    m = re.search(r"ollama_duel\.py\s+(\S+)", cmdline)
    if m:
        return os.path.basename(m.group(1)).removesuffix(".json").replace(" ", "_")
    return f"pid-{pid}"


def track_duels(w, pids):
    cur = " ".join(f"{p}:{duel_name(p)}" for p in pids)
    prev = w.state.get("duel_pids", "") or ""
    if cur == prev:
        return
    cur_set = cur.split() if cur else []
    prev_set = prev.split() if prev else []
    delivered = True
    for pair in cur_set:
        if pair not in prev_set:
            entry = pair.split(":", 1)[1]
            if not w.notify(2, f"fleet-watch duel started [{w.hostname}]",
                            f"Duel started on {w.hostname}: {entry}."):
                delivered = False
            w.log(f"duel started: {entry}")
    for pair in prev_set:
        if pair not in cur_set:
            entry = pair.split(":", 1)[1]
            if not w.notify(2, f"fleet-watch duel ended [{w.hostname}]",
                            f"Duel ended on {w.hostname}: {entry}."):
                delivered = False
            w.log(f"duel ended: {entry}")
    if delivered:
        w.state.set("duel_pids", cur)
    w.report.append(f"[duels] tracked: {cur or 'none'} (was: {prev or 'none'})")


def check_duels(w, now=None):
    now = now or int(time.time())
    pids = _pgrep("[o]llama_duel.py")
    track_duels(w, pids)
    if not pids:
        w.report.append("[duels] none running")
        return
    w.report.append(f"[duels] pids: {' '.join(pids)}")
    duel_dir = w.cfg.get("DUEL_DIR", os.path.expanduser("~/dual"))
    age_m = None
    log_path = os.path.join(duel_dir, "run_results.log")
    if os.path.exists(log_path):
        age_m = (now - int(os.path.getmtime(log_path))) // 60
        w.report.append(f"[duels] run_results.log age: {age_m}m")
    else:
        w.report.append(f"[duels] no run_results.log in {duel_dir}")
    logs = sorted(glob.glob(os.path.join(duel_dir, "output/logs/*.log")),
                  key=lambda p: os.path.getmtime(p), reverse=True)
    if logs:
        newest_age = (now - int(os.path.getmtime(logs[0]))) // 60
        w.report.append(
            f"[duels] newest transcript: {os.path.basename(logs[0])} "
            f"({newest_age}m old)")
        if age_m is None or newest_age < age_m:
            age_m = newest_age
    age_m = age_m or 0
    maxcpu = 0
    for p in _pgrep("[l]lama-server"):
        r = subprocess.run(["ps", "-o", "%cpu=", "-p", p],
                           capture_output=True, text=True)
        try:
            maxcpu = max(maxcpu, int(float((r.stdout or "0").strip())))
        except ValueError:
            pass
    w.report.append(f"[duels] llama-server max cpu: {maxcpu}%")
    stuck = cfg_int(w.cfg, "STUCK_AFTER_MIN")
    if age_m > stuck and maxcpu > 70:
        w.flags.append(
            f"crit:duel_stuck:no duel output for {age_m}m while llama-server "
            f"pegged at {maxcpu}% (pids {' '.join(pids)})")
    elif age_m > stuck:
        w.flags.append(
            f"warn:duel_idle:no duel output for {age_m}m, llama-server idle "
            "(probably loading next model)")


# ------------------------------------------------------------------ peer

def peer_check(w, now=None):
    now = now or int(time.time())
    peer = w.cfg.get("PEER_HOST", "")
    fails = int(w.state.get("peer_fail_count", "0") or 0)
    rc, out = w.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                     peer, "cat ~/.fleet-watch/heartbeat"], timeout=30)
    rts = (out or "").strip()
    if rc == 0 and rts.isdigit():
        age = (now - int(rts)) // 60
        w.report.append(f"[peer {peer}] heartbeat age: {age}m")
        w.log(f"peer {peer}: heartbeat age {age}m")
        if age > cfg_int(w.cfg, "INTERVAL_MIN") * 3:
            w.flags.append(f"warn:peer_stale:{peer} heartbeat {age}m old")
        w.state.set("peer_fail_count", 0)
    else:
        fails += 1
        w.state.set("peer_fail_count", fails)
        w.report.append(
            f"[peer {peer}] heartbeat unreachable (consecutive fail {fails})")
        w.log(f"peer {peer}: heartbeat unreachable (consecutive fail {fails})")
        if fails >= 2:
            w.flags.append(
                f"crit:peer_silent:{peer} silent for 2 consecutive checks")


# --------------------------------------------------------------- fileList

def filelist_web_code(w, ip):
    rc, out = w.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                     "-m", "5", f"http://{ip}:{w.cfg.get('FILELIST_PORT', '8081')}/"],
                    timeout=15)
    code = (out or "").strip()
    return code if code else "000"


def filelist_worker_state(w):
    host = w.cfg.get("FILELIST_WORKER_HOST", "")
    rc, out = w.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                     host,
                     'pgrep -f "[j]ob_worker.py" >/dev/null 2>&1 && '
                     "echo up || echo down"], timeout=30)
    val = (out or "").strip()
    return val if val in ("up", "down") else "unreachable"


def filelist_eval(w, ip, code, worker):
    label = f"filelist-{ip}"
    port = w.cfg.get("FILELIST_PORT", "8081")
    if code and code != "000":
        w.unreach_clear(label)
        w.report.append(f"[filelist {ip}] web answering (HTTP {code})")
        w.triage_summary.append(f"- {ip}: fileList web OK (HTTP {code})")
    else:
        w.report.append(f"[filelist {ip}] web NOT answering")
        w.triage_summary.append(f"- {ip}: fileList web not answering")
        if w.unreach_should_flag(label):
            w.flags.append(
                f"warn:filelist:{ip} fileList web UI not answering on "
                f"http://{ip}:{port}/ (2+ consecutive checks)")
        else:
            w.log(f"filelist {ip}: first consecutive web miss (debounced)")
    if worker == "up":
        w.unreach_clear("filelist-worker")
        w.report.append(f"[filelist {ip}] job worker running")
    elif worker == "down":
        w.report.append(f"[filelist {ip}] job worker NOT running")
        w.triage_summary.append(f"- {ip}: fileList job worker NOT running")
        if w.unreach_should_flag("filelist-worker"):
            w.flags.append(
                f"warn:filelist:{ip} fileList job worker (job_worker.py) "
                "not running — queued jobs will sit stranded")
        else:
            w.log("filelist worker: first consecutive miss (debounced)")
    elif worker == "unreachable":
        w.report.append(
            f"[filelist {ip}] job worker state unknown (SSH to "
            f"{w.cfg.get('FILELIST_WORKER_HOST', '')} failed)")


def check_filelist(w):
    hosts = w.cfg.get("FILELIST_HOSTS", "").split()
    if not hosts:
        return
    worker_host = w.cfg.get("FILELIST_WORKER_HOST", "")
    worker_ip = worker_host.rsplit("@", 1)[-1] if worker_host else ""
    for ip in hosts:
        code = filelist_web_code(w, ip)
        worker = "skip"
        if worker_ip and ip == worker_ip:
            worker = filelist_worker_state(w)
        filelist_eval(w, ip, code, worker)


# ---------------------------------------------------------------- miners

def miner_present_set(w):
    present = set()
    for ip in w.cfg.get("MINER_IPS", "").split():
        try:
            req = urllib.request.Request(f"http://{ip}/swarm")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.load(resp)
            present.add(ip)
            for dev in data.get("devices", []):
                dip = str(dev.get("ip", ""))
                if dip:
                    present.add(dip)
        except Exception:
            continue
    return " ".join(sorted(present))


def in_list(item, lst):
    return item in lst.split()


def miners_eval(w, present):
    seen = w.state.get("miners_seen", "") or ""
    down = w.state.get("miners_down", "") or ""
    new_seen, new_down = seen, ""
    for ip in w.cfg.get("MINER_IPS", "").split():
        if in_list(ip, present):
            w.unreach_clear(f"miner-{ip}")
            if not in_list(ip, new_seen):
                new_seen = (new_seen + " " + ip).strip()
            if in_list(ip, down):
                w.report.append(f"[miners] {ip} back in the swarm (recovered)")
                w.triage_summary.append(f"- {ip}: NerdMiner back in the swarm")
                w.log(f"miner {ip} recovered")
            else:
                w.triage_summary.append(f"- {ip}: NerdMiner in swarm")
        elif in_list(ip, down):
            new_down = (new_down + " " + ip).strip()
            w.triage_summary.append(f"- {ip}: NerdMiner still out of the swarm")
        elif in_list(ip, seen):
            w.triage_summary.append(f"- {ip}: NerdMiner missing from swarm")
            if w.unreach_should_flag(f"miner-{ip}"):
                w.flags.append(
                    f"warn:miner_down:{ip} NerdMiner has dropped out of the "
                    "swarm (absent 2+ consecutive checks)")
                new_down = (new_down + " " + ip).strip()
        else:
            w.report.append(
                f"[miners] {ip} not present (never seen — not alerting)")
            w.triage_summary.append(f"- {ip}: NerdMiner never seen (not alerting)")
    w.state.set("miners_seen", new_seen.strip())
    w.state.set("miners_down", new_down.strip())
    w.report.append(
        f"[miners] present:{present or ' none'} | down:{new_down.strip() or ' none'}")


def check_miners(w):
    if not w.cfg.get("MINER_IPS", "").split():
        return
    miners_eval(w, miner_present_set(w))


# ----------------------------------------------------------- failed units

UNIT_SUFFIXES = (".service", ".mount", ".timer", ".socket", ".target",
                 ".path", ".slice", ".scope", ".device", ".swap", ".automount")


def failed_units_parse(text):
    units = set()
    for ln in text.splitlines():
        ln = re.sub(r"^[^A-Za-z0-9_@.:-]+", "", ln)
        tok = ln.split()
        if tok and tok[0].endswith(UNIT_SUFFIXES):
            units.add(tok[0])
    return sorted(units)


def failed_units_eval(w, host, cur_units):
    key = "failed_units_" + state_key_for(host)
    cur_csv = ",".join(cur_units)
    if w.state.get(key) is None:
        w.state.set(key, cur_csv)
        w.report.append(
            f"[failed-units {host}] baseline recorded (failed now: "
            f"{cur_csv or 'none'} — pre-existing, not alerting)")
        w.log(f"failed-units {host}: baseline recorded ({cur_csv or 'none'})")
        return
    known = (w.state.get(key) or "").split(",") if w.state.get(key) else []
    new = [u for u in cur_units if u not in known]
    w.state.set(key, cur_csv)
    new_list = " ".join(new)
    if new_list:
        w.flags.append(
            f"warn:failed_new:{host} newly failed systemd unit(s): {new_list}")
    w.report.append(
        f"[failed-units {host}] failed now: {cur_csv or 'none'} | "
        f"new: {new_list or 'none'}")


def check_failed_units(w, now=None):
    now = now or int(time.time())
    hosts = w.cfg.get("FAILED_UNIT_HOSTS", "").split()
    if not hosts:
        return
    last = int(w.state.get("failed_units_last_run", "0") or 0)
    interval = cfg_int(w.cfg, "FAILED_UNITS_INTERVAL_H") * 3600
    if now - last < interval:
        w.report.append(
            f"[failed-units] daily check not due "
            f"(last run {(now - last) // 3600}h ago)")
        return
    w.state.set("failed_units_last_run", now)
    for host in hosts:
        rc, out = w.run(["ssh", "-o", "BatchMode=yes", "-o",
                         "ConnectTimeout=8", host,
                         "systemctl --failed --no-legend 2>/dev/null || true"],
                        timeout=30)
        if rc == 0:
            failed_units_eval(w, host, failed_units_parse(out))
        else:
            w.report.append(
                f"[failed-units {host}] SSH FAILED — cannot check "
                "(coverage gap)")
            w.log(f"failed-units {host}: ssh failed (coverage gap)")


# --------------------------------------------------------------- usenet

def usenet_probe(w, host):
    rc1, a = w.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                    host, USENET_PROBE_PCPU], timeout=30)
    if rc1 != 0:
        return None
    rc2, b = w.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                    host, USENET_PROBE_DB], timeout=90)
    if rc2 != 0:
        return None
    return a + "\n" + b


def usenet_eval(w, out, now):
    host = w.cfg.get("USENET_HOST", "")
    kv = {}
    for ln in out.splitlines():
        if "=" in ln:
            k, _, v = ln.partition("=")
            kv[k.strip()] = v.strip()
    running = kv.get("running", "0")
    pending = kv.get("pending", "")
    cpu = kv.get("cpu", "0")
    mtime = kv.get("mtime", "0")
    if running != "1":
        w.state.set("usenet_sample", "")
        w.state.set("usenet_sample_time", 0)
        w.report.append(
            f"[usenet] usenetFetch not running on {host} "
            "(normal — stall check silent)")
        return
    if not re.fullmatch(r"-?\d+", pending or "") or pending == "-1":
        w.report.append(
            f"[usenet] usenetFetch running on {host} but pending count "
            "unavailable — stall check skipped")
        return
    if int(pending) <= 0:
        w.state.set("usenet_sample", "")
        w.state.set("usenet_sample_time", 0)
        w.report.append(
            f"[usenet] usenetFetch running on {host}, pending=0 "
            "(nothing waiting — stall check silent)")
        return
    sample = f"{pending}|{cpu}|{mtime}"
    prev = w.state.get("usenet_sample", "") or ""
    prev_time = int(w.state.get("usenet_sample_time", "0") or 0)
    if sample != prev:
        w.state.set("usenet_sample", sample)
        w.state.set("usenet_sample_time", now)
        w.report.append(
            f"[usenet] usenetFetch alive on {host}: pending={pending}, "
            "signs of life current")
        return
    idle_min = (now - prev_time) // 60
    w.report.append(
        f"[usenet] usenetFetch on {host}: pending={pending} unchanged "
        f"for {idle_min}m")
    if idle_min >= cfg_int(w.cfg, "USENET_STALL_MIN"):
        w.flags.append(
            f"warn:usenet_stall:{host} usenetFetch appears stalled: process "
            f"running, {pending} releases pending, but no DB writes, no CPU "
            f"progress and no queue change for {idle_min}m")


def check_usenet_stall(w, now=None):
    now = now or int(time.time())
    host = w.cfg.get("USENET_HOST", "")
    if not host:
        return
    out = usenet_probe(w, host)
    if out is not None:
        usenet_eval(w, out, now)
    else:
        w.report.append(
            f"[usenet] cannot probe {host} over SSH (coverage gap)")
        w.log(f"usenet: ssh probe to {host} failed (coverage gap)")


# ---------------------------------------------------------------- triage

def worst_sev(flags):
    sev = "OK"
    for f in flags:
        s = f.split(":", 1)[0]
        if s == "crit":
            return "CRIT"
        if s == "warn":
            sev = "WARN"
    return sev


def rule_verdict(flags):
    return [f"- {f.split(':', 2)[2] if f.count(':') >= 2 else f}" for f in flags]


def flag_kind(flag):
    parts = flag.split(":", 2)
    return parts[1] if len(parts) > 1 else ""


def flag_rest(flag):
    parts = flag.split(":", 2)
    return parts[2] if len(parts) > 2 else ""


def flag_group(w, flag):
    kind = flag_kind(flag)
    if kind in ("duel_stuck", "duel_idle"):
        return w.self_label
    if kind == "triage_endpoint":
        return "triage"
    rest = flag_rest(flag)
    return rest.split(" ", 1)[0] if rest else ""


def flag_mount(flag):
    words = flag_rest(flag).split(" ")
    return words[1] if len(words) > 1 else ""


def group_key_for(w, flag):
    if flag_kind(flag) == "disk":
        return f"{flag_group(w, flag)}|disk|{flag_mount(flag)}"
    return flag_group(w, flag)


def group_flags(flags, key, w):
    return [f for f in flags if group_key_for(w, f) == key]


def group_flags_text(w, key):
    return "\n".join(f"- {flag_rest(f)}" for f in group_flags(w.flags, key, w))


def mount_section(detail, mount):
    out = []
    active = False
    for ln in detail.splitlines():
        if ln.startswith("== "):
            if active:
                break
            if ln == f"== topdirs {mount}":
                active = True
        if active:
            out.append(ln)
    return "\n".join(out)


def hint_for(kind, mount=""):
    table = {
        "disk": f"Check: du -sh {mount}/* 2>/dev/null | sort -rh | head",
        "inodes": f"Check: df -i {mount or '/'}",
        "failedsvc": "Check: systemctl --failed, then journalctl -u <unit> -n 50 for the one that matters",
        "smart": "Check: sudo smartctl -a <device> for the full SMART report",
        "readonly": 'Check: dmesg | grep -iE "error|remount" for what forced it read-only',
        "dmesg": 'Check: dmesg | grep -iE "I/O error|EXT4-fs error" | tail',
        "temp": "Check: uptime for load; vcgencmd measure_temp or sensors for cooling",
        "ollama_down": "Check: systemctl status ollama; curl -s localhost:11434/api/tags",
        "oom": 'Check: dmesg | grep -i "killed process" to see what was killed',
        "clock": "Check: timedatectl; systemctl status systemd-timesyncd",
        "duel_stuck": "Check: tail the newest log in ~/dual/output/logs",
        "duel_idle": "Check: tail the newest log in ~/dual/output/logs",
        "filelist": "Check: curl -sI http://<host>:8081/ for the web side; on the Vostro, ps aux | grep job_worker for the worker",
        "miner_down": "Check: ping the miner, then curl -s http://<ip>/swarm to see whether it answers at all",
        "failed_new": "Check: systemctl status <unit> on the host, then journalctl -u <unit> -n 50",
        "usenet_stall": "Check: on the Vostro, ps aux | grep usenet-fetch and tail ~/usenetFetch/daily_log.txt",
        "peer_silent": "Check: ssh to the peer and tail ~/.fleet-watch/fleet-watch.log",
        "peer_stale": "Check: ssh to the peer and tail ~/.fleet-watch/fleet-watch.log",
        "unreachable": "Check: ping the host, then ssh -v to find the failure point",
        "reboot_required": "Note: reboot when convenient to finish pending updates",
    }
    return table.get(kind, "")


def group_hint(w, key):
    mount = key.split("|disk|", 1)[1] if "|disk|" in key else ""
    for f in group_flags(w.flags, key, w):
        return hint_for(flag_kind(f), mount)
    return ""


def build_group_input(w, key):
    label = key.split("|", 1)[0]
    mount = key.split("|disk|", 1)[1] if "|disk|" in key else ""
    lines = [f"# FINDINGS (only these — one host{', one filesystem' if mount else ''})"]
    has_duel = False
    for f in group_flags(w.flags, key, w):
        lines.append(f"- {flag_rest(f)}")
        if flag_kind(f) in ("duel_stuck", "duel_idle"):
            has_duel = True
    for ln in w.triage_summary:
        if ln.startswith(f"- {label}:"):
            lines.append("# HOST SUMMARY")
            lines.append(ln)
            break
    if has_duel:
        lines.append("# DUEL STATE")
        lines.extend(l for l in w.report if l.startswith("[duels]"))
    peer = w.cfg.get("PEER_HOST", "")
    if peer and label == peer:
        lines.extend([l for l in w.report if l.startswith("[peer ")][:1])
    if mount:
        lines.append(f"# DISK DETAIL — largest entries on {mount} ONLY")
        lines.append(mount_section(w.detail_by_host.get(label, ""), mount))
    return "\n".join(lines) + "\n"


def call_model(w, group_file):
    prompt = ""
    if os.path.exists(w.prompt_file):
        with open(w.prompt_file, encoding="utf-8", errors="replace") as f:
            prompt = f.read()
    with open(group_file, encoding="utf-8", errors="replace") as f:
        report = f.read()
    body = json.dumps({
        "model": w.cfg.get("MODEL"),
        "prompt": prompt + "\n\n# CURRENT FINDINGS — one issue group "
                  "(times America/Chicago)\n" + report,
        "stream": False,
        "think": False,
        "options": {"num_predict": 150},
    }).encode()
    req = urllib.request.Request(
        w.cfg.get("OLLAMA_HOST", "") + "/api/generate", data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.load(resp)["response"]
    except Exception as e:  # degrade to raw flags, like the bash version
        err_path = os.path.join(w.state_dir, "triage_err.txt")
        with open(err_path, "w", encoding="utf-8") as f:
            f.write(str(e))
        return ""


def strip_model_text(text):
    kept = []
    for ln in text.splitlines():
        if re.match(r"(VERDICT|SUGGESTION):", ln.strip(), re.I):
            continue
        ln = ln.strip()
        if ln:
            kept.append(ln)
    return "\n".join(kept[:4])


def normalize_dedup_key(key):
    key = re.sub(r"[0-9]+m( old)?", "", key)
    key = re.sub(r"(at|pegged at) [0-9]+%", "", key)
    key = re.sub(r"\(pids [0-9 ]+\)", "", key)
    return key


def maybe_alert(w, sev, reason, full, key=None):
    """6h same-issue suppression on a single stored hash, as the bash
    version: a failed delivery does NOT advance the dedup state."""
    if key is None:
        key = f"{sev}|{reason}"
    digest = hashlib.md5(normalize_dedup_key(key).encode()).hexdigest()
    now = int(time.time())
    last_hash = w.state.get("last_alert_hash", "none") or "none"
    last_time = int(w.state.get("last_alert_time", "0") or 0)
    if digest == last_hash and now - last_time < 21600:
        w.log(f"alert suppressed (same issue <6h): {reason}")
        return True
    pri, tags = (4, "rotating_light") if sev == "CRIT" else (3, "warning")
    if not w.notify(pri, f"fleet-watch {sev} [{w.hostname}]", full, tags):
        return False
    w.state.set("last_alert_hash", digest)
    w.state.set("last_alert_time", now)
    w.state.set("last_verdict", sev)
    return True


# ------------------------------------------------------------------ main

def run(w):
    w.rotate_log()
    w.log(f"run start (DRY_RUN={1 if w.dry_run else 0})")
    w.log_block("command PROBE", PROBE_TEMPLATE + "\n")
    w.log_block("command DEEP_DISK", DEEP_DISK + "\n")
    stamp = datetime.now().strftime("%F %T %Z")
    header = f"# fleet-watch run {stamp}" + (" BETA" if w.beta else "")
    w.report.append(header)
    now = int(time.time())

    check_host(w, "localhost", w.self_label, now)
    peer = w.cfg.get("PEER_HOST", "")
    for h in w.cfg.get("FLEET_HOSTS", "").split():
        if h == peer:
            continue
        check_host(w, h, h, now)
    check_duels(w, now)
    peer_check(w, now)
    check_filelist(w)
    check_miners(w)
    check_failed_units(w, now)
    check_usenet_stall(w, now)

    report_text = "\n".join(w.report) + "\n"
    with open(w.report_file, "w", encoding="utf-8") as f:
        f.write(report_text)
    w.log_block("report", report_text)

    overview = "# FLAGS (what fired this run)\n"
    overview += "\n".join(rule_verdict(w.flags))
    if w.flags:
        overview += "\n"
    overview += "# HOST SUMMARIES (one line each)\n"
    overview += "\n".join(w.triage_summary) + "\n"
    triage_file = os.path.join(w.state_dir, "triage_input.txt")
    with open(triage_file, "w", encoding="utf-8") as f:
        f.write(overview)
    w.log_block("triage overview", overview)

    endpoint_down = False
    rc, _ = w.run(["curl", "-s", "-m", "10",
                   w.cfg.get("OLLAMA_HOST", "") + "/api/tags",
                   "-o", "/dev/null"], timeout=20)
    if rc != 0:
        endpoint_down = True
        w.flags.append(
            f"warn:triage_endpoint:OLLAMA_HOST "
            f"{w.cfg.get('OLLAMA_HOST', '')} not answering — triage "
            "degraded to rule-based")

    sev = worst_sev(w.flags)
    last = w.state.get("last_verdict", "OK") or "OK"

    if sev == "OK":
        recovery_delivered = True
        if last != "OK":
            if w.notify(2, f"fleet-watch recovered [{w.hostname}]",
                        f"All checks clear again. Previous state: {last}.",
                        "white_check_mark"):
                w.log(f"recovered (was {last})")
            else:
                recovery_delivered = False
        lp = int(w.state.get("last_daily_ping", "0") or 0)
        if now - lp > 86400:
            if w.notify(1, f"fleet-watch alive [{w.hostname}]",
                        "Daily check-in: all fleet checks clear.", "zzz"):
                w.state.set("last_daily_ping", now)
        if recovery_delivered:
            w.state.set("last_verdict", "OK")
        w.log("OK")
    else:
        ollama_host = w.cfg.get("OLLAMA_HOST", "")
        local_triage = ollama_host.startswith(
            ("http://localhost", "http://127.", "https://localhost",
             "https://127."))
        allow_triage = True
        if local_triage and _pgrep("[o]llama_duel.py"):
            r = subprocess.run(["ollama", "ps"], capture_output=True, text=True)
            if w.cfg.get("MODEL", "") not in (r.stdout or ""):
                allow_triage = False
                w.log("triage model calls skipped: duel active with "
                      "another model loaded")
        if endpoint_down:
            allow_triage = False

        group_keys = []
        for f in w.flags:
            k = group_key_for(w, f)
            if k not in group_keys:
                group_keys.append(k)
        if allow_triage:
            w.log(f"triage: calling {w.cfg.get('MODEL')} at {ollama_host} "
                  f"for {len(group_keys)} issue group(s)")
        body = ""
        model_any = False
        for key in group_keys:
            g_label = key.split("|", 1)[0]
            g_mount = key.split("|disk|", 1)[1] if "|disk|" in key else ""
            g_file = os.path.join(
                w.state_dir,
                f"triage_input.{state_key_for(key)}.txt")
            g_input = build_group_input(w, key)
            with open(g_file, "w", encoding="utf-8") as f:
                f.write(g_input)
            w.log_block(f"triage input [{key}]", g_input)
            g_answer = ""
            if allow_triage:
                g_answer = call_model(w, g_file)
                w.log_block(f"triage response [{key}]", g_answer + "\n")
                if not g_answer:
                    err = ""
                    err_path = os.path.join(w.state_dir, "triage_err.txt")
                    if os.path.exists(err_path):
                        with open(err_path, encoding="utf-8",
                                  errors="replace") as f:
                            err = f.read()[:300].replace("\n", " ")
                    w.log(f"triage call failed for {key}: {err}")
            g_text = strip_model_text(g_answer)
            heading = f"[{g_label} — disk {g_mount}]" if g_mount else f"[{g_label}]"
            body += heading + "\n"
            if g_text:
                model_any = True
                body += g_text + "\n"
            else:
                body += group_flags_text(w, key) + "\n"
            hint = group_hint(w, key)
            if hint:
                body += hint + "\n"
        if model_any:
            full = f"fleet-watch {sev} — per-issue triage:\n" + body
        else:
            full = (f"fleet-watch {sev} (model triage unavailable — raw "
                    f"findings):\n" + body)
        verdict_lines = rule_verdict(w.flags)
        reason = verdict_lines[0][2:][:160] if verdict_lines else ""
        maybe_alert(w, sev, reason, full,
                    f"{sev}|" + ";".join(verdict_lines))
        w.log(f"{sev}: {reason}")

    w.log(f"run end: {sev}")
    with open(w.heartbeat_file, "w", encoding="utf-8") as f:
        f.write(f"{int(time.time())}\n")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    beta = "--beta" in argv or os.environ.get("FW_BETA") == "1"
    w = Watcher(beta=beta)
    if not w.cfg.get("PEER_HOST") or not w.cfg.get("NTFY_URL"):
        print("fleet-watch: set PEER_HOST and NTFY_URL in fleet-watch.conf",
              file=sys.stderr)
        return 1
    lock_path = os.path.join(w.state_dir, "lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("fleet-watch: another run in progress, exiting")
        return 0
    return run(w)


if __name__ == "__main__":
    sys.exit(main())
