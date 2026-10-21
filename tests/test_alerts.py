"""Exercise only shell functions with fake curl/state; no fleet or ntfy calls."""
import itertools
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest

BASH = os.environ.get('BASH_EXECUTABLE') or shutil.which('bash')
SOURCE = (Path(__file__).resolve().parents[1] / 'fleet-watch.sh').read_text(encoding='utf-8')


def function(name):
    match = re.search(r'^' + name + r'\(\).*?^}', SOURCE, re.M | re.S)
    if not match:
        raise AssertionError('missing shell function: ' + name)
    return match.group(0)


@unittest.skipUnless(BASH, 'Bash required')
class AlertTests(unittest.TestCase):
    def shell(self, code, functions=()):
        script = '\n'.join(function(name) for name in functions) + '\n' + code
        result = subprocess.run([BASH, '--noprofile', '--norc', '-c', script],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_any_critical_flag_wins_regardless_of_order(self):
        for flags in itertools.permutations(['crit:disk:full', 'warn:temp:hot', 'warn:mem:low']):
            with self.subTest(flags=flags):
                code = 'FLAGS=(' + ' '.join(flags) + '); worst_sev'
                self.assertEqual(self.shell(code, ['worst_sev']), 'CRIT')

    def test_empty_and_warning_flags(self):
        self.assertEqual(self.shell('FLAGS=(); worst_sev', ['worst_sev']), 'OK')
        self.assertEqual(self.shell('FLAGS=(warn:disk:high); worst_sev', ['worst_sev']), 'WARN')

    def test_notify_propagates_failure_and_asks_curl_to_fail_on_http_errors(self):
        for status in (0, 1, 22):
            code = '''
log() { :; }
curl() { case "$1" in *f*) ;; *) return 99;; esac; return STATUS; }
DRY_RUN=0; NTFY_URL=https://unused.invalid
notify 4 title body; printf '%s' "$?"
'''.replace('STATUS', str(status))
            self.assertEqual(self.shell(code, ['notify']), '0' if status == 0 else '1')

    def test_failed_delivery_does_not_advance_dedup_state_and_can_retry(self):
        code = '''
log() { :; }
state_get() { :; }
state_set() { printf 'saved:%s\\n' "$1"; }
notify() { return 1; }
maybe_alert CRIT problem body key || true
notify() { return 0; }
maybe_alert CRIT problem body key
'''
        result = self.shell(code, ['maybe_alert'])
        self.assertEqual(result.splitlines(), ['saved:last_alert_hash', 'saved:last_alert_time', 'saved:last_verdict'])

    def test_successful_delivery_suppresses_the_same_recent_alert(self):
        code = '''
log() { :; }
state_get() { case "$1" in last_alert_hash) printf '%s' "$saved_hash";; last_alert_time) date +%s;; esac; }
state_set() { case "$1" in last_alert_hash) saved_hash=$2;; esac; }
notify() { echo delivered; }
saved_hash=none
maybe_alert CRIT problem body key
maybe_alert CRIT problem body key
'''
        self.assertEqual(self.shell(code, ['maybe_alert']), 'delivered')


if __name__ == '__main__':
    unittest.main()
