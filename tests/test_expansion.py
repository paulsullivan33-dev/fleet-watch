"""Expansion checks (2026-11-02): fileList health, NerdMiner drop-off,
daily newly-failed systemd units, usenetFetch stall. Same extraction
style as the other suites — shell functions run in isolation with
stubbed probes and temp state files. No fleet, no HTTP, no ntfy.

Facts these tests encode, all verified live on 2026-11-02:
- fileList answers HTTP 302 on :8081 on .119/.118/.103/.248; its job
  worker is the process job_worker.py, and it died silently for a day
  (2026-11-01/02) while the web UI stayed up, stranding a queued job.
- Each NerdMiner serves /swarm JSON whose devices list names the whole
  swarm (.125/.154/.188/.190); .154 hung that day and only a desk
  display noticed.
- The Q/Pi watcher accounts can SSH to .119/.118/.248 but NOT to
  .236/.103/.185/.165/.179 — unreachable check targets are coverage
  gaps, never alerts.
- usenetFetch's pending queue is SELECT COUNT(*) FROM pending_releases
  (16,918 at the last live read, process not running — the normal,
  silent state). Its DB runs in WAL mode, so the freshest write time is
  max(db mtime, wal mtime).
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

BASH = os.environ.get('BASH_EXECUTABLE') or shutil.which('bash')
ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'fleet-watch.sh').read_text(encoding='utf-8')

STATE_FNS = ['state_get', 'state_set', 'state_key_for',
             'unreach_should_flag', 'unreach_clear']
PRELUDE = """
log() { :; }
FLAGS=()
REPORT=""
TRIAGE_SUMMARY=""
FILELIST_PORT=8081
USENET_HOST='paul@192.168.1.119'
USENET_STALL_MIN=60
FAILED_UNITS_INTERVAL_H=23
dump_flags() { for f in "${FLAGS[@]}"; do echo "FLAG: $f"; done; }
"""


def function(name):
    match = re.search(r'^' + name + r'\(\).*?^}', SOURCE, re.M | re.S)
    if not match:
        raise AssertionError('missing shell function: ' + name)
    return match.group(0)


@unittest.skipUnless(BASH, 'Bash required')
class ExpansionTests(unittest.TestCase):
    def shell(self, code, functions=()):
        script = ('\n'.join(function(name) for name in functions)
                  + '\n' + PRELUDE + '\n' + code)
        result = subprocess.run([BASH, '--noprofile', '--norc', '-c', script],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    # -- wiring / config -------------------------------------------------

    def test_main_calls_all_four_checks_and_defaults_exist(self):
        for name in ('check_filelist', 'check_miners',
                     'check_failed_units', 'check_usenet_stall'):
            # definition + at least one call site in main
            self.assertGreaterEqual(SOURCE.count(name), 2, name)
        for var in ('FILELIST_HOSTS', 'FILELIST_PORT', 'FILELIST_WORKER_HOST',
                    'MINER_IPS', 'FAILED_UNIT_HOSTS', 'FAILED_UNITS_INTERVAL_H',
                    'USENET_HOST', 'USENET_STALL_MIN'):
            self.assertIn(': "${%s:=' % var, SOURCE, var)

    def test_new_flag_kinds_group_by_host_label(self):
        code = """
flag_group 'warn:filelist:192.168.1.119 fileList web UI not answering'; echo
flag_group 'warn:miner_down:192.168.1.154 NerdMiner has dropped out'; echo
flag_group 'warn:failed_new:paul@192.168.1.185 newly failed systemd unit(s): x.service'; echo
flag_group 'warn:usenet_stall:paul@192.168.1.119 usenetFetch appears stalled'; echo
"""
        fns = ['flag_kind', 'flag_rest', 'flag_group']
        self.assertEqual(self.shell(code, fns).splitlines(), [
            '192.168.1.119', '192.168.1.154',
            'paul@192.168.1.185', 'paul@192.168.1.119'])

    def test_new_hints_exist_and_are_read_only(self):
        code = """
for k in filelist miner_down failed_new usenet_stall; do
  hint_for "$k" ''
  echo
done
"""
        out = self.shell(code, ['hint_for'])
        self.assertNotRegex(out, r'\brm\b|delete|vacuum|truncate|mkfs|remove|restart|kill')
        for needle in ('curl', 'swarm', 'journalctl', 'daily_log.txt'):
            self.assertIn(needle, out)

    # -- fileList --------------------------------------------------------

    def test_filelist_web_code_wraps_curl(self):
        code = """
curl() { printf '302'; return 0; }
echo "ok=$(filelist_web_code 192.168.1.119)"
curl() { printf '000'; return 7; }
echo "refused=$(filelist_web_code 192.168.1.119)"
curl() { return 7; }
echo "silent=$(filelist_web_code 192.168.1.119)"
"""
        out = self.shell(code, ['filelist_web_code'])
        self.assertEqual(out.splitlines(), ['ok=302', 'refused=000', 'silent=000'])

    def test_filelist_healthy_run_flags_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            code = """
STATE_FILE='%s'
filelist_eval 192.168.1.119 302 up
filelist_eval 192.168.1.118 302 skip
dump_flags
printf '%%s' "$REPORT"
""" % str(Path(td) / 'state')
            fns = STATE_FNS + ['filelist_eval']
            out = self.shell(code, fns)
            self.assertNotIn('FLAG:', out)
            self.assertIn('web answering (HTTP 302)', out)
            self.assertIn('job worker running', out)

    def test_filelist_web_down_alerts_only_after_second_miss(self):
        with tempfile.TemporaryDirectory() as td:
            code = """
STATE_FILE='%s'
filelist_eval 192.168.1.119 000 skip
echo "after1 flags=${#FLAGS[@]}"
filelist_eval 192.168.1.119 000 skip
dump_flags
""" % str(Path(td) / 'state')
            fns = STATE_FNS + ['filelist_eval']
            out = self.shell(code, fns)
            self.assertIn('after1 flags=0', out)
            self.assertIn('fileList web UI not answering', out)

    def test_filelist_worker_down_alerts_after_debounce(self):
        # The 2026-11-01/02 incident: web up, worker dead, job stranded.
        with tempfile.TemporaryDirectory() as td:
            code = """
STATE_FILE='%s'
filelist_eval 192.168.1.119 302 down
filelist_eval 192.168.1.119 302 down
dump_flags
""" % str(Path(td) / 'state')
            fns = STATE_FNS + ['filelist_eval']
            out = self.shell(code, fns)
            self.assertIn('job worker (job_worker.py) not running', out)
            self.assertIn('stranded', out)
            self.assertEqual(out.count('FLAG:'), 1)

    def test_filelist_worker_unreachable_is_a_note_not_a_flag(self):
        with tempfile.TemporaryDirectory() as td:
            code = """
STATE_FILE='%s'
filelist_eval 192.168.1.119 302 unreachable
filelist_eval 192.168.1.119 302 unreachable
filelist_eval 192.168.1.119 302 unreachable
dump_flags
printf '%%s' "$REPORT"
""" % str(Path(td) / 'state')
            fns = STATE_FNS + ['filelist_eval']
            out = self.shell(code, fns)
            self.assertNotIn('FLAG:', out)
            self.assertIn('worker state unknown', out)

    # -- NerdMiners ------------------------------------------------------

    def test_miner_present_set_unions_devices_from_any_responder(self):
        code = """
MINER_IPS='192.168.1.125 192.168.1.154 192.168.1.188 192.168.1.190'
miner_swarm_json() {
  case "$1" in
    192.168.1.125) printf '%s' '{"devices":[{"ip":"192.168.1.125"},{"ip":"192.168.1.154"},{"ip":"192.168.1.188"},{"ip":"192.168.1.190"}]}' ;;
    *) return 1 ;;
  esac
}
miner_present_set
echo
"""
        out = self.shell(code, ['miner_swarm_json', 'miner_present_set'])
        self.assertEqual(out.split(), ['192.168.1.125', '192.168.1.154',
                                       '192.168.1.188', '192.168.1.190'])

    def test_miner_dropout_debounce_recovery_and_refailure(self):
        with tempfile.TemporaryDirectory() as td:
            code = """
STATE_FILE='%s'
MINER_IPS='192.168.1.125 192.168.1.154 192.168.1.188 192.168.1.190'
ALL='192.168.1.125 192.168.1.154 192.168.1.188 192.168.1.190'
MISS='192.168.1.125 192.168.1.188 192.168.1.190'
miners_eval "$ALL"
echo "seen=$(state_get miners_seen)"
miners_eval "$MISS"
echo "miss1 flags=${#FLAGS[@]}"
miners_eval "$MISS"
echo "miss2 flags=${#FLAGS[@]} down=$(state_get miners_down)"
miners_eval "$MISS"
echo "miss3 flags=${#FLAGS[@]}"
miners_eval "$ALL"
echo "back flags=${#FLAGS[@]} down=[$(state_get miners_down)]"
dump_flags
""" % str(Path(td) / 'state')
            fns = STATE_FNS + ['in_list', 'miners_eval']
            out = self.shell(code, fns)
            self.assertIn('seen=192.168.1.125 192.168.1.154 192.168.1.188 192.168.1.190', out)
            self.assertIn('miss1 flags=0', out)
            self.assertIn('miss2 flags=1 down=192.168.1.154', out)
            self.assertIn('miss3 flags=1', out)  # already down: no repeat flag
            self.assertIn('back flags=1 down=[]', out)
            self.assertIn('NerdMiner has dropped out of the swarm', out)

    def test_miner_never_seen_never_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            code = """
STATE_FILE='%s'
MINER_IPS='192.168.1.125 192.168.1.154 192.168.1.188 192.168.1.190 192.168.1.200'
PRESENT='192.168.1.125 192.168.1.154 192.168.1.188 192.168.1.190'
miners_eval "$PRESENT"
miners_eval "$PRESENT"
miners_eval "$PRESENT"
dump_flags
printf '%%s' "$REPORT"
""" % str(Path(td) / 'state')
            fns = STATE_FNS + ['in_list', 'miners_eval']
            out = self.shell(code, fns)
            self.assertNotIn('FLAG:', out)
            self.assertIn('never seen', out)

    # -- Failed systemd units (daily) ------------------------------------

    def test_failed_units_parse_strips_bullets_and_sorts(self):
        code = """
{ echo '● casper-md5check.service loaded failed failed Verify Live ISO checksums'
  echo '  zzz.service               loaded failed failed Zzz'
  echo '● bar.mount                 loaded failed failed Bar mount'
  echo 'not a unit line'
} | failed_units_parse
echo "empty=[$(printf '' | failed_units_parse)]"
"""
        out = self.shell(code, ['failed_units_parse'])
        self.assertEqual(out.splitlines(), [
            'bar.mount', 'casper-md5check.service', 'zzz.service', 'empty=[]'])

    def test_failed_units_baseline_then_only_new_alert(self):
        with tempfile.TemporaryDirectory() as td:
            code = """
STATE_FILE='%s'
H='paul@192.168.1.185'
failed_units_eval "$H" 'a.service'
echo "base flags=${#FLAGS[@]}"
failed_units_eval "$H" 'a.service'
echo "same flags=${#FLAGS[@]}"
failed_units_eval "$H" 'a.service
b.service'
failed_units_eval "$H" ''
failed_units_eval "$H" 'b.service'
echo "refail flags=${#FLAGS[@]}"
dump_flags
""" % str(Path(td) / 'state')
            fns = STATE_FNS + ['failed_units_eval']
            out = self.shell(code, fns)
            self.assertIn('base flags=0', out)   # pre-existing: silent baseline
            self.assertIn('same flags=0', out)
            flags = [ln for ln in out.splitlines() if ln.startswith('FLAG:')]
            self.assertEqual(len(flags), 2)      # b.service new, then re-failed
            self.assertIn('b.service', flags[0])
            self.assertNotIn('a.service', flags[0])
            self.assertIn('refail flags=2', out)

    def test_failed_units_daily_gate(self):
        with tempfile.TemporaryDirectory() as td:
            count = str(Path(td) / 'count')
            code = """
STATE_FILE='%s'
FAILED_UNIT_HOSTS='paul@h1 paul@h2'
failed_units_probe() { echo x >> '%s'; printf '%%s\\n' 'a.service'; }
check_failed_units
echo "run1 probes=$(wc -l < '%s')"
check_failed_units
echo "run2 probes=$(wc -l < '%s')"
state_set failed_units_last_run 100
check_failed_units
echo "run3 probes=$(wc -l < '%s')"
dump_flags
""" % (str(Path(td) / 'state'), count, count, count, count)
            fns = STATE_FNS + ['failed_units_probe', 'failed_units_parse',
                               'failed_units_eval', 'check_failed_units']
            out = self.shell(code, fns)
            self.assertIn('run1 probes=2', out)
            self.assertIn('run2 probes=2', out)  # gate: not due, no probing
            self.assertIn('run3 probes=4', out)
            self.assertNotIn('FLAG:', out)       # baselined, nothing new

    def test_failed_units_ssh_gap_is_report_only(self):
        with tempfile.TemporaryDirectory() as td:
            state = str(Path(td) / 'state')
            code = """
STATE_FILE='%s'
FAILED_UNIT_HOSTS='paul@h1 paul@h2'
failed_units_probe() {
  case "$1" in *h2*) return 255 ;; *) printf '%%s\\n' 'a.service' ;; esac
}
check_failed_units
dump_flags
printf '%%s' "$REPORT"
echo '--- state ---'
cat "$STATE_FILE"
""" % state
            fns = STATE_FNS + ['failed_units_probe', 'failed_units_parse',
                               'failed_units_eval', 'check_failed_units']
            out = self.shell(code, fns)
            self.assertNotIn('FLAG:', out)
            self.assertIn('coverage gap', out)
            self.assertIn('failed_units_paul_h1=a.service', out)
            self.assertNotIn('failed_units_paul_h2', out)

    # -- usenetFetch stall -----------------------------------------------

    def test_usenet_not_running_is_silent_and_resets_streak(self):
        with tempfile.TemporaryDirectory() as td:
            code = """
STATE_FILE='%s'
usenet_eval 'running=1
pending=16918
cpu=500
mtime=1793647979' 1000000
usenet_eval 'running=0' 1000600
echo "sample=[$(state_get usenet_sample)]"
dump_flags
printf '%%s' "$REPORT"
""" % str(Path(td) / 'state')
            fns = STATE_FNS + ['usenet_eval']
            out = self.shell(code, fns)
            self.assertNotIn('FLAG:', out)
            self.assertIn('sample=[]', out)
            self.assertIn('not running', out)

    def test_usenet_stall_needs_full_frozen_window(self):
        with tempfile.TemporaryDirectory() as td:
            code = """
STATE_FILE='%s'
S='running=1
pending=16918
cpu=500
mtime=1793647979'
usenet_eval "$S" 1000000
echo "t0 flags=${#FLAGS[@]}"
usenet_eval "$S" 1001800
echo "t30 flags=${#FLAGS[@]}"
usenet_eval "$S" 1003600
echo "t60 flags=${#FLAGS[@]}"
S2='running=1
pending=16918
cpu=505
mtime=1793647979'
usenet_eval "$S2" 1004200
usenet_eval "$S2" 1006000
echo "after-progress flags=${#FLAGS[@]}"
dump_flags
""" % str(Path(td) / 'state')
            fns = STATE_FNS + ['usenet_eval']
            out = self.shell(code, fns)
            self.assertIn('t0 flags=0', out)     # first sighting just samples
            self.assertIn('t30 flags=0', out)    # frozen 30m < 60m window
            self.assertIn('t60 flags=1', out)    # all three frozen a full hour
            self.assertIn('after-progress flags=1', out)  # CPU moved: streak reset
            self.assertIn('usenetFetch appears stalled', out)
            self.assertIn('16918 releases pending', out)

    def test_usenet_pending_zero_and_unavailable_are_silent(self):
        with tempfile.TemporaryDirectory() as td:
            code = """
STATE_FILE='%s'
usenet_eval 'running=1
pending=0
cpu=5
mtime=1793647979' 1000000
usenet_eval 'running=1
pending=-1
cpu=5
mtime=1793647979' 1000600
dump_flags
printf '%%s' "$REPORT"
""" % str(Path(td) / 'state')
            fns = STATE_FNS + ['usenet_eval']
            out = self.shell(code, fns)
            self.assertNotIn('FLAG:', out)
            self.assertIn('pending=0', out)
            self.assertIn('pending count unavailable', out)

    def test_usenet_probe_split_defeats_pgrep_self_match(self):
        # The process probe's command line is visible to its own pgrep on
        # the remote host; it must contain the pattern ONLY in bracketed
        # form, and nothing from the DB probe (whose text names the DB
        # file, which itself contains the pattern as a substring).
        pcpu = re.search(r"^USENET_PROBE_PCPU='(.*)'$", SOURCE, re.M).group(1)
        self.assertIn('[u]senet-fetch', pcpu)
        self.assertNotIn('usenet-fetch.db', pcpu)
        self.assertNotIn('sqlite3', pcpu)
        self.assertNotIn('pgrep -f "usenet-fetch"', SOURCE)
        db = re.search(r"^USENET_PROBE_DB='(.*)'$", SOURCE, re.M).group(1)
        self.assertIn('pending_releases', db)
        self.assertIn('mode=ro', db)
        self.assertIn('-wal', db)  # WAL mtime counts: freshest write wins


if __name__ == '__main__':
    unittest.main()
