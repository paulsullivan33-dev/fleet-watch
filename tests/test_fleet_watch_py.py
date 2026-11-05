"""Unit tests for fleet_watch.py (the Python port of fleet-watch.sh).

Pure-logic tests with stubbed transports — no fleet, SSH, or ntfy access.
Run: python3 -m unittest tests.test_fleet_watch_py (or pytest tests/).
"""
import itertools
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import fleet_watch as fw


def make_watcher(tmp, conf_text=""):
    conf = os.path.join(tmp, "fleet-watch.conf")
    with open(conf, "w") as f:
        f.write('PEER_HOST="paul@192.168.1.109"\n')
        f.write('NTFY_URL="https://ntfy.example.invalid/topic"\n')
        f.write(conf_text)
    return fw.Watcher(beta=True, conf_path=conf, home=tmp)


class ConfigTests(unittest.TestCase):
    def test_conf_quotes_comments_and_home_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = make_watcher(tmp, 'MODEL="qwen3.5:4b"  \n# comment\n'
                                  'DUEL_DIR="$HOME/dual"\nDISK_WARN=90\n')
            self.assertEqual(w.cfg["MODEL"], "qwen3.5:4b")
            self.assertEqual(w.cfg["DUEL_DIR"], os.path.expanduser("~/dual"))
            self.assertEqual(fw.cfg_int(w.cfg, "DISK_WARN"), 90)
            # default fills an unset key
            self.assertEqual(fw.cfg_int(w.cfg, "DISK_CRIT"), 95)


class RunnerTests(unittest.TestCase):
    def test_run_merges_stderr_and_returns_rc(self):
        rc, out = fw.Watcher.run(["bash", "-c", "echo out; echo err >&2; exit 3"])
        self.assertEqual(rc, 3)
        self.assertIn("out", out)
        self.assertIn("err", out)


class WorstSevTests(unittest.TestCase):
    def test_crit_wins_regardless_of_order(self):
        for flags in itertools.permutations(
                ["crit:disk:full", "warn:temp:hot", "warn:mem:low"]):
            self.assertEqual(fw.worst_sev(list(flags)), "CRIT")

    def test_empty_and_warn(self):
        self.assertEqual(fw.worst_sev([]), "OK")
        self.assertEqual(fw.worst_sev(["warn:disk:high"]), "WARN")


class NormalizeTests(unittest.TestCase):
    def test_volatile_measurements_stripped(self):
        a = fw.normalize_dedup_key(
            "WARN|- paul@h / at 86%\n- no duel output for 5161m while "
            "llama-server pegged at 366% (pids 12 34)")
        b = fw.normalize_dedup_key(
            "WARN|- paul@h / at 91%\n- no duel output for 5171m while "
            "llama-server pegged at 240% (pids 56)")
        self.assertEqual(a, b)

    def test_severity_transition_changes_key(self):
        self.assertNotEqual(fw.normalize_dedup_key("WARN|x at 86%"),
                            fw.normalize_dedup_key("CRIT|x at 86%"))


class MaybeAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.w = make_watcher(self.tmp.name)
        self.sent = []
        self.fail = False

        def fake_notify(pri, title, msg, tags="satellite"):
            if self.fail:
                return False
            self.sent.append((pri, title))
            return True
        self.w.notify = fake_notify

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_sends_second_suppressed(self):
        self.assertTrue(fw.maybe_alert(self.w, "WARN", "r", "body", "WARN|k"))
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(fw.maybe_alert(self.w, "WARN", "r", "body", "WARN|k"))
        self.assertEqual(len(self.sent), 1)  # <6h suppression

    def test_failed_delivery_does_not_advance_state(self):
        self.fail = True
        self.assertFalse(fw.maybe_alert(self.w, "CRIT", "r", "body", "CRIT|k"))
        self.assertIsNone(self.w.state.get("last_alert_hash"))
        self.fail = False
        self.assertTrue(fw.maybe_alert(self.w, "CRIT", "r", "body", "CRIT|k"))
        self.assertEqual(len(self.sent), 1)

    def test_different_issue_sends(self):
        fw.maybe_alert(self.w, "WARN", "r1", "b", "WARN|one")
        fw.maybe_alert(self.w, "WARN", "r2", "b", "WARN|two")
        self.assertEqual(len(self.sent), 2)


class FailedUnitsTests(unittest.TestCase):
    def test_baseline_then_new_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = make_watcher(tmp)
            fw.failed_units_eval(w, "paul@h", ["a.service"])
            self.assertEqual(w.flags, [])  # baseline is silent
            fw.failed_units_eval(w, "paul@h", ["a.service"])
            self.assertEqual(w.flags, [])
            fw.failed_units_eval(w, "paul@h", ["a.service", "b.service"])
            self.assertEqual(len(w.flags), 1)
            self.assertIn("b.service", w.flags[0])
            # steady state again: no repeat
            fw.failed_units_eval(w, "paul@h", ["a.service", "b.service"])
            self.assertEqual(len(w.flags), 1)

    def test_parse_filters_non_units(self):
        out = "● foo.service loaded failed failed Foo\n  bar.mount x\nrandom\n"
        self.assertEqual(fw.failed_units_parse(out),
                         ["bar.mount", "foo.service"])


class MinersTests(unittest.TestCase):
    def test_seen_then_absent_flags_on_second_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = make_watcher(tmp, 'MINER_IPS="192.168.1.125"\n')
            fw.miners_eval(w, "192.168.1.125")     # seen
            self.assertEqual(w.flags, [])
            fw.miners_eval(w, "")                  # first miss: debounced
            self.assertEqual(w.flags, [])
            fw.miners_eval(w, "")                  # second miss: flags
            self.assertEqual(len(w.flags), 1)
            self.assertIn("miner_down", w.flags[0])
            fw.miners_eval(w, "192.168.1.125")     # recovery
            self.assertEqual(w.state.get("miners_down"), "")

    def test_never_seen_never_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = make_watcher(tmp, 'MINER_IPS="192.168.1.125"\n')
            for _ in range(3):
                fw.miners_eval(w, "")
            self.assertEqual(w.flags, [])


class UsenetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.w = make_watcher(self.tmp.name, 'USENET_STALL_MIN=60\n')

    def tearDown(self):
        self.tmp.cleanup()

    def test_not_running_is_silent(self):
        fw.usenet_eval(self.w, "running=0\n", 1000)
        self.assertEqual(self.w.flags, [])

    def test_stall_needs_frozen_sample_for_full_interval(self):
        out = "running=1\ncpu=50\nmtime=900\npending=7\n"
        fw.usenet_eval(self.w, out, 1000)          # first sample stored
        self.assertEqual(self.w.flags, [])
        fw.usenet_eval(self.w, out, 1000 + 30 * 60)  # 30m frozen: no flag
        self.assertEqual(self.w.flags, [])
        fw.usenet_eval(self.w, out, 1000 + 61 * 60)  # 61m frozen: flags
        self.assertEqual(len(self.w.flags), 1)
        self.assertIn("usenet_stall", self.w.flags[0])

    def test_sign_of_life_resets_clock(self):
        fw.usenet_eval(self.w, "running=1\ncpu=50\nmtime=900\npending=7\n", 1000)
        fw.usenet_eval(self.w, "running=1\ncpu=80\nmtime=900\npending=7\n",
                       1000 + 3600)  # cpu moved -> alive, no flag
        self.assertEqual(self.w.flags, [])


OOM_OUT = """== oom
uptime_sec={up}
{kill_lines}
== end
"""


class OomTests(unittest.TestCase):
    NOW = 1_700_000_000

    def out(self, up, kills):
        lines = "\n".join(
            f"[{t}.100] Out of memory: Killed process 1 (llama-server)"
            for t in kills)
        return OOM_OUT.format(up=up, kill_lines=lines)

    def test_flags_once_then_remembers(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = make_watcher(tmp)
            fw.oom_check(w, "host", self.out(100000, [50000]), self.NOW)
            self.assertEqual(len(w.flags), 1)
            self.assertIn("oom", w.flags[0])
            w.flags.clear()
            # same kill, 10 minutes later (uptime advanced in step)
            fw.oom_check(w, "host", self.out(100600, [50000]), self.NOW + 600)
            self.assertEqual(w.flags, [])  # remembered: no re-flag

    def test_newer_kill_flags_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = make_watcher(tmp)
            fw.oom_check(w, "host", self.out(100000, [50000]), self.NOW)
            w.flags.clear()
            fw.oom_check(w, "host", self.out(100600, [50000, 90000]),
                         self.NOW + 600)
            self.assertEqual(len(w.flags), 1)

    def test_kill_outside_window_never_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = make_watcher(tmp)
            fw.oom_check(w, "host", self.out(100000, [1000]), self.NOW)
            self.assertEqual(w.flags, [])

    def test_reboot_resets_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = make_watcher(tmp)
            fw.oom_check(w, "host", self.out(100000, [50000]), self.NOW)
            w.flags.clear()
            # rebooted: uptime small again, a fresh in-window kill
            fw.oom_check(w, "host", self.out(5000, [4000]), self.NOW + 3600)
            self.assertEqual(len(w.flags), 1)


class GroupingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.w = make_watcher(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_disk_flags_group_by_mount(self):
        f1 = "warn:disk:paul@h /data at 90%"
        f2 = "warn:temp:paul@h CPU temp at or above 80C"
        self.assertEqual(fw.group_key_for(self.w, f1), "paul@h|disk|/data")
        self.assertEqual(fw.group_key_for(self.w, f2), "paul@h")

    def test_hint_for_disk_uses_mount(self):
        self.assertEqual(fw.hint_for("disk", "/data"),
                         "Check: du -sh /data/* 2>/dev/null | sort -rh | head")

    def test_strip_model_text(self):
        text = "VERDICT: bad\nReal prose line.\nSUGGESTION: do x\n\nAnother.\n"
        self.assertEqual(fw.strip_model_text(text), "Real prose line.\nAnother.")


class FilelistEvalTests(unittest.TestCase):
    def test_web_miss_debounced_then_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = make_watcher(tmp)
            fw.filelist_eval(w, "192.168.1.119", "000", "skip")
            self.assertEqual(w.flags, [])
            fw.filelist_eval(w, "192.168.1.119", "000", "skip")
            self.assertEqual(len(w.flags), 1)
            fw.filelist_eval(w, "192.168.1.119", "302", "skip")
            self.assertEqual(
                w.state.get("unreach_filelist_192_168_1_119"), "0")


if __name__ == "__main__":
    unittest.main()
