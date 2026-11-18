"""Raw-evidence alert tests: alerts must carry the verbatim probe /
report lines behind each flagged issue, inline, under a 'Raw evidence:'
block — capped, with an explicit truncation marker, and absent when
there are no findings.

Regression fixture: the 2026-11-17 incident. On paul@192.168.1.85 (no
curl installed) the probe's Ollama check emitted
'bash: line 1: curl: command not found' on stderr; the line landed in
the failedsvc section, failed_svc_info() took its first token ('bash:')
as a service name, and the alert prose reported a failed systemd
service named bash. The raw block must show that line verbatim.

Run: python3 -m unittest discover -s tests (or pytest tests/).
"""
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


HOST85 = "paul@192.168.1.85"

# The .85 probe output as the Q watcher captured it in the 16:54 run:
# the failedsvc section holds ONLY the curl error line.
INCIDENT_OUT = """== uptime
 16:54:01 up 6 days,  3:14,  1 user,  load average: 0.05, 0.04, 0.01
== df
Filesystem      Size  Used Avail Use% Mounted on
/dev/sda1        30G   13G   17G  44% /
== mem
Mem:           4000        1000        2500           0         500        2800
== cores
2
== temp
42.0 C
== oom
uptime_sec=527040
== ollama
api=down
== failedsvc
bash: line 1: curl: command not found
== rebootreq
no
== inodes
12
== smart
== ro
== dmesg
== clock
yes
== end
"""


class IncidentRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # .85 does not run Ollama: keep it unmonitored so the probe's
        # api=down token adds no ollama flag, as in the real run.
        self.w = make_watcher(self.tmp.name,
                              'OLLAMA_HOSTS="paul@192.168.1.236"\n')
        self.orig_probe = fw.run_probe
        fw.run_probe = lambda w, host: (0, INCIDENT_OUT)

    def tearDown(self):
        fw.run_probe = self.orig_probe
        self.tmp.cleanup()

    def test_check_host_flags_bash_colon_and_retains_probe(self):
        fw.check_host(self.w, HOST85, HOST85, now=1700000000)
        self.assertEqual(self.w.probe_by_label[HOST85], INCIDENT_OUT)
        self.assertEqual(len(self.w.flags), 1)
        self.assertIn("failedsvc", self.w.flags[0])
        self.assertIn("bash:", self.w.flags[0])

    def test_raw_block_shows_curl_line_verbatim(self):
        fw.check_host(self.w, HOST85, HOST85, now=1700000000)
        key = fw.group_key_for(self.w, self.w.flags[0])
        text = fw.render_evidence(self.w, key, [fw.EVIDENCE_BUDGET_CHARS])
        self.assertIn("Raw evidence:", text)
        self.assertIn("[failedsvc]", text)
        # The whole point: the artifact line, byte for byte.
        self.assertIn("\nbash: line 1: curl: command not found\n", text)
        self.assertNotIn(fw.EVIDENCE_TRUNCATED, text)


class NoEvidenceTests(unittest.TestCase):
    def test_no_findings_no_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = make_watcher(tmp)
            self.assertEqual(fw.evidence_sources_for(w, HOST85), [])
            self.assertEqual(
                fw.render_evidence(w, HOST85, [fw.EVIDENCE_BUDGET_CHARS]),
                "")

    def test_flag_without_retained_raw_lines_no_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = make_watcher(tmp)
            w.flags.append(
                "warn:triage_endpoint:OLLAMA_HOST http://localhost:11434 "
                "not answering — triage degraded to rule-based")
            key = fw.group_key_for(w, w.flags[0])
            self.assertEqual(key, "triage")
            self.assertEqual(
                fw.render_evidence(w, key, [fw.EVIDENCE_BUDGET_CHARS]),
                "")


class TruncationTests(unittest.TestCase):
    def _watcher_with_dmesg(self, tmp, lines):
        w = make_watcher(tmp)
        out = "== dmesg\n" + "\n".join(lines) + "\n== end\n"
        w.probe_by_label["paul@h"] = out
        w.flags.append(
            f"warn:dmesg:paul@h {len(lines)} disk/filesystem error(s) "
            "in dmesg")
        return w

    def test_line_cap_marks_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines = [f"[  100.0] EXT4-fs error (device sda1): ext4 err {i}"
                     for i in range(20)]
            w = self._watcher_with_dmesg(tmp, lines)
            text = fw.render_evidence(w, "paul@h",
                                      [fw.EVIDENCE_BUDGET_CHARS])
            self.assertTrue(text.rstrip().endswith(fw.EVIDENCE_TRUNCATED))
            kept = [ln for ln in text.splitlines()
                    if ln.startswith("[  100.0]")]
            self.assertEqual(len(kept), fw.EVIDENCE_LINES_PER_SOURCE)

    def test_char_cap_marks_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines = ["x" * 100 + f" {i}" for i in range(6)]
            w = self._watcher_with_dmesg(tmp, lines)
            text = fw.render_evidence(w, "paul@h",
                                      [fw.EVIDENCE_BUDGET_CHARS])
            self.assertIn(fw.EVIDENCE_TRUNCATED, text)
            kept = [ln for ln in text.splitlines() if ln.startswith("x")]
            self.assertLess(len(kept), 6)
            self.assertLessEqual(
                sum(len(ln) + 1 for ln in kept),
                fw.EVIDENCE_CHARS_PER_SOURCE)

    def test_total_budget_starves_later_group_to_marker_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = make_watcher(tmp)
            # 99-char lines: the per-source cap keeps 4 (400 chars), so
            # each group's block costs ~408 of the 1200-char budget —
            # two groups fit, the third is starved to the marker alone.
            lines = ["[  200.0] I/O error " + "y" * 79 for _ in range(6)]
            labels = ("paul@h1", "paul@h2", "paul@h3")
            for label in labels:
                w.probe_by_label[label] = (
                    "== dmesg\n" + "\n".join(lines) + "\n== end\n")
                w.flags.append(
                    f"warn:dmesg:{label} 6 disk/filesystem error(s) "
                    "in dmesg")
            budget = [fw.EVIDENCE_BUDGET_CHARS]
            first = fw.render_evidence(w, "paul@h1", budget)
            self.assertIn("I/O error", first)
            self.assertLess(budget[0], fw.EVIDENCE_BUDGET_CHARS)
            second = fw.render_evidence(w, "paul@h2", budget)
            self.assertIn("I/O error", second)
            third = fw.render_evidence(w, "paul@h3", budget)
            self.assertEqual(
                third,
                f"Raw evidence:\n{fw.EVIDENCE_TRUNCATED}\n")
            # The whole alert's raw lines stay within the budget.
            spent = fw.EVIDENCE_BUDGET_CHARS - budget[0]
            self.assertLessEqual(spent, fw.EVIDENCE_BUDGET_CHARS)


class SourceSelectionTests(unittest.TestCase):
    def test_disk_evidence_is_only_the_flagged_mounts_df_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = make_watcher(tmp)
            w.probe_by_label["paul@h"] = (
                "== df\n"
                "Filesystem      Size  Used Avail Use% Mounted on\n"
                "/dev/sda1       100G   44G   56G  44% /\n"
                "/dev/sdb1       500G  460G   40G  92% /data\n"
                "== end\n")
            w.flags.append("crit:disk:paul@h /data at 92%")
            key = fw.group_key_for(w, w.flags[0])
            self.assertEqual(key, "paul@h|disk|/data")
            text = fw.render_evidence(w, key, [fw.EVIDENCE_BUDGET_CHARS])
            self.assertIn("/dev/sdb1", text)
            self.assertIn("/data", text)
            self.assertNotIn("/dev/sda1", text)

    def test_unreachable_evidence_is_the_ssh_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = make_watcher(tmp)
            label = "paul@192.168.1.94"
            w.probe_by_label[label] = (
                "ssh: connect to host 192.168.1.94 port 22: "
                "No route to host\n")
            w.flags.append(
                f"crit:unreachable:{label} did not answer SSH "
                "(2+ consecutive runs)")
            text = fw.render_evidence(w, label, [fw.EVIDENCE_BUDGET_CHARS])
            self.assertIn("[ssh output]", text)
            self.assertIn("No route to host", text)

    def test_duplicate_flags_share_one_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = make_watcher(tmp)
            w.probe_by_label[HOST85] = INCIDENT_OUT
            flag = (f"warn:failedsvc:{HOST85} has 1 failed systemd "
                    "service(s): bash:")
            w.flags.extend([flag, flag])
            sources = fw.evidence_sources_for(w, HOST85)
            self.assertEqual([s for s, _ in sources], ["failedsvc"])

    def test_report_line_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = make_watcher(tmp)
            w.report.append(
                "[failed-units paul@h] failed now: b.service | "
                "new: b.service")
            w.flags.append(
                "warn:failed_new:paul@h newly failed systemd unit(s): "
                "b.service")
            text = fw.render_evidence(w, "paul@h",
                                      [fw.EVIDENCE_BUDGET_CHARS])
            self.assertIn("[failed-units paul@h] failed now: b.service",
                          text)


if __name__ == "__main__":
    unittest.main()
