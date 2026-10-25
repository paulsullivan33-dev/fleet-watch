"""Split-triage tests: grouping, per-filesystem input scoping, hint fencing,
prompt fencing, and the unreachable debounce. Same extraction style as
test_alerts.py — shell functions run in isolation with canned state.

The disk fixture reconstructs the 2026-10-24 false-triage case from the
fleet-watch log's documented facts: on the Vostro, /myData/share1 tripped
at 87% with sabnzbd (6.0T) as its largest entry, while the home-directory
sections of the same deep-disk output listed usenetFetch (213G) on the
root filesystem. The old single-call triage blamed usenetFetch for the
share1 flag; the scoped per-filesystem input must make that impossible.
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
PROMPT = (ROOT / 'triage-prompt.txt').read_text(encoding='utf-8')

DETAIL_LINES = [
    '== du',
    '200G /home/paul/.ollama/models',
    '5.0G /home/paul/dual/logs',
    '== topdirs',
    '213G /home/paul/usenetFetch',
    '40G /home/paul/Downloads',
    '== journal',
    'Archived and active journals take up 1.2G in the file system.',
    '== models',
    'qwen3.5:4b 4.2 GB',
    '== topdirs /myData/share1',
    '6.0T /myData/share1/sabnzbd',
    '300G /myData/share1/media',
    '== topdirs /myData/share2',
    '900G /myData/share2/backups',
]


def function(name):
    match = re.search(r'^' + name + r'\(\).*?^}', SOURCE, re.M | re.S)
    if not match:
        raise AssertionError('missing shell function: ' + name)
    return match.group(0)


@unittest.skipUnless(BASH, 'Bash required')
class TriageTests(unittest.TestCase):
    def shell(self, code, functions=()):
        script = '\n'.join(function(name) for name in functions) + '\n' + code
        result = subprocess.run([BASH, '--noprofile', '--norc', '-c', script],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_group_keys(self):
        code = '''
SELF_LABEL='self(testhost)'
group_key_for 'warn:disk:paul@paul-Vostro-260a /myData/share1 at 87%'; echo
group_key_for 'warn:failedsvc:paul@paul-OptiPlex-7010 has 1 failed systemd service(s): rtkit-daemon.service'; echo
group_key_for 'crit:duel_stuck:no duel output for 130m while llama-server pegged at 366% (pids 1234)'; echo
group_key_for 'crit:peer_silent:paul@192.168.1.109 silent for 2 consecutive checks'; echo
group_key_for 'warn:triage_endpoint:OLLAMA_HOST http://192.168.1.236:11434 not answering'; echo
'''
        fns = ['flag_kind', 'flag_rest', 'flag_group', 'flag_mount', 'group_key_for']
        out = self.shell(code, fns).splitlines()
        self.assertEqual(out, [
            'paul@paul-Vostro-260a|disk|/myData/share1',
            'paul@paul-OptiPlex-7010',
            'self(testhost)',
            'paul@192.168.1.109',
            'triage',
        ])

    def test_mount_section_extracts_only_that_mount(self):
        printf_args = ' '.join("'%s'" % line for line in DETAIL_LINES)
        code = '''
DETAIL=$(printf '%s\\n' __ARGS__)
echo '--- share1 ---'
mount_section "$DETAIL" /myData/share1
echo '--- share2 ---'
mount_section "$DETAIL" /myData/share2
echo '--- missing ---'
mount_section "$DETAIL" /nonexistent
echo '--- end ---'
'''.replace('__ARGS__', printf_args)
        out = self.shell(code, ['mount_section'])
        share1 = out.split('--- share1 ---')[1].split('--- share2 ---')[0]
        self.assertIn('sabnzbd', share1)
        self.assertIn('6.0T', share1)
        self.assertNotIn('usenetFetch', share1)
        self.assertNotIn('backups', share1)
        self.assertNotIn('journal', share1)
        share2 = out.split('--- share2 ---')[1].split('--- missing ---')[0]
        self.assertIn('backups', share2)
        self.assertNotIn('sabnzbd', share2)
        missing = out.split('--- missing ---')[1].split('--- end ---')[0]
        self.assertEqual(missing.strip(), '')

    def _group_env(self):
        printf_args = ' '.join("'%s'" % line for line in DETAIL_LINES)
        return '''
SELF_LABEL='self(testhost)'
PEER_HOST='paul@192.168.1.171'
FLAGS=(
 'warn:disk:paul@paul-Vostro-260a /myData/share1 at 87%'
 'warn:failedsvc:paul@paul-OptiPlex-7010 has 1 failed systemd service(s): rtkit-daemon.service'
 'crit:duel_stuck:no duel output for 130m while llama-server pegged at 366% (pids 1234)'
)
TRIAGE_SUMMARY='- paul@paul-Vostro-260a: up 30 days | worst disk: 87% (/myData/share1) | mem: 4000M avail | ollama: api_http=200
- paul@paul-OptiPlex-7010: up 12 days | worst disk: 14% (/) | mem: 2000M avail | ollama: api_http=200 | failed_svc:1(rtkit-daemon.service)
- self(testhost): up 9 days | worst disk: 40% (/) | mem: 900M avail | ollama: api_http=200
'
REPORT='[duels] pids: 1234
[duels] run_results.log age: 130m
'
declare -A DETAIL_BY_HOST=()
DETAIL_BY_HOST['paul@paul-Vostro-260a']=$(printf '%s\\n' __ARGS__)
'''.replace('__ARGS__', printf_args)

    def test_disk_group_input_excludes_home_sections(self):
        code = self._group_env() + '''
build_group_input 'paul@paul-Vostro-260a|disk|/myData/share1'
'''
        fns = ['flag_kind', 'flag_rest', 'flag_group', 'flag_mount',
               'group_key_for', 'mount_section', 'build_group_input']
        out = self.shell(code, fns)
        self.assertIn('/myData/share1 at 87%', out)
        self.assertIn('sabnzbd', out)
        # The Oct 24 conflation, made impossible: nothing from the home
        # sections, nothing from other hosts, nothing about duels.
        self.assertNotIn('usenetFetch', out)
        self.assertNotIn('Downloads', out)
        self.assertNotIn('rtkit', out)
        self.assertNotIn('duel', out)

    def test_host_group_input_has_no_disk_detail(self):
        code = self._group_env() + '''
build_group_input 'paul@paul-OptiPlex-7010'
'''
        fns = ['flag_kind', 'flag_rest', 'flag_group', 'flag_mount',
               'group_key_for', 'mount_section', 'build_group_input']
        out = self.shell(code, fns)
        self.assertIn('rtkit-daemon.service', out)
        self.assertNotIn('sabnzbd', out)
        self.assertNotIn('DISK DETAIL', out)

    def test_duel_group_input_includes_duel_state(self):
        code = self._group_env() + '''
build_group_input 'self(testhost)'
'''
        fns = ['flag_kind', 'flag_rest', 'flag_group', 'flag_mount',
               'group_key_for', 'mount_section', 'build_group_input']
        out = self.shell(code, fns)
        self.assertIn('no duel output for 130m', out)
        self.assertIn('[duels] pids: 1234', out)
        self.assertNotIn('sabnzbd', out)

    def test_hints_are_read_only(self):
        code = '''
for k in disk inodes failedsvc smart readonly dmesg temp ollama_down oom clock duel_stuck peer_silent unreachable reboot_required; do
  hint_for "$k" /myData/share1
  echo
done
'''
        out = self.shell(code, ['hint_for'])
        self.assertNotRegex(out, r'\brm\b|delete|vacuum|truncate|mkfs|remove')
        self.assertIn('du -sh /myData/share1/*', out)

    def test_prompt_is_fenced(self):
        # No destructive examples or output demands anywhere in the file
        # the model reads — header comments included.
        self.assertNotIn('ollama rm', PROMPT)
        self.assertNotIn('vacuum', PROMPT.lower())
        self.assertNotIn('VERDICT:', PROMPT)
        self.assertNotIn('SUGGESTION:', PROMPT)
        self.assertIn('no suggestions', PROMPT)

    def test_unreachable_debounce(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / 'state')
            code = '''
STATE_FILE='%s'
unreach_should_flag 'paul@paul-Vostro-260a'; echo "first=$?"
unreach_should_flag 'paul@paul-Vostro-260a'; echo "second=$?"
unreach_clear 'paul@paul-Vostro-260a'
unreach_should_flag 'paul@paul-Vostro-260a'; echo "after_clear=$?"
''' % state_file
            fns = ['state_get', 'state_set', 'state_key_for',
                   'unreach_should_flag', 'unreach_clear']
            out = self.shell(code, fns)
            self.assertEqual(out.splitlines(),
                             ['first=1', 'second=0', 'after_clear=1'])


if __name__ == '__main__':
    unittest.main()
