"""Service checks and MACHINES host coverage (2026-11-15).

Pure-logic tests with stubbed transports — no fleet, SSH, HTTP or ntfy.

Facts these tests encode, verified live on 2026-11-15:
- dxticker's /health on the Vostro (:8086) returns version,
  mqtt_connected and last_msg_age_s; it sends its own ntfy alert after
  30 min without spots but cannot report its own death.
- upstream runs from a systemd user timer every 2 h on the Vostro;
  `systemctl --user show -p Result --value upstream.service` is
  "success" after a good run, and LastTriggerUSec on the timer dates the
  last firing.
- ~/MACHINES on the Vostro lists 15 hosts as paul@IP lines, including
  both watchers. Both watchers could SSH to 9 of them; .85, .94, .130 and
  .150 failed with "Host key verification failed" (never set up), and
  each watcher gets "Permission denied (publickey,password)" from its own
  address. Those are login refusals, not outages.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import fleet_watch as fw

NOW = 1_794_750_000


def make_watcher(tmp, conf_text=""):
    conf = os.path.join(tmp, "fleet-watch.conf")
    with open(conf, "w") as f:
        f.write('PEER_HOST="paul@192.168.1.171"\n')
        f.write('NTFY_URL="https://ntfy.example.invalid/topic"\n')
        f.write(conf_text)
    return fw.Watcher(beta=True, conf_path=conf, home=tmp)


class StubRun:
    """Replaces Watcher.run: maps a predicate on the argv to (rc, out)."""

    def __init__(self, rules):
        self.rules = rules
        self.calls = []

    def __call__(self, cmd, timeout=60):
        self.calls.append(cmd)
        joined = " ".join(cmd)
        for needle, result in self.rules:
            if needle in joined:
                return result
        return 255, "ssh: connect to host x port 22: No route to host"


class DefaultsTests(unittest.TestCase):
    def test_new_checks_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = make_watcher(tmp, 'FLEET_HOSTS="paul@192.168.1.119"\n')
            for k in ("DXTICKER_HEALTH_URL", "UPSTREAM_HOST", "MACHINES_SOURCE",
                      "HOST_EXCLUDE"):
                self.assertEqual(w.cfg[k], "")
            w.run = StubRun([])
            fw.check_dxticker(w, NOW)
            fw.check_upstream(w, NOW)
            self.assertEqual(w.flags, [])
            # Default host list is exactly FLEET_HOSTS: no SSH, no hostname -I.
            self.assertEqual(fw.fleet_hosts(w, NOW), ["paul@192.168.1.119"])
            self.assertEqual(w.run.calls, [])

    def test_run_wires_new_checks(self):
        src = open(fw.__file__).read()
        run_body = src[src.index("def run(w):"):src.index("def main(")]
        for call in ("fleet_hosts(w, now)", "check_dxticker(w, now)",
                     "check_upstream(w, now)"):
            self.assertIn(call, run_body)


class DxtickerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.w = make_watcher(
            self.tmp.name,
            'DXTICKER_HEALTH_URL="http://192.168.1.119:8086/health"\n')

    def tearDown(self):
        self.tmp.cleanup()

    def healthy(self, **kw):
        h = {"ok": True, "version": "0.2.0", "mqtt_connected": True,
             "last_msg_age_s": 2}
        h.update(kw)
        return h

    def test_healthy_is_silent(self):
        fw.dxticker_eval(self.w, self.healthy(), NOW)
        self.assertEqual(self.w.flags, [])
        self.assertIn("- dxticker: OK (v0.2.0)", self.w.triage_summary)

    def test_unreachable_debounced_then_flags(self):
        fw.dxticker_eval(self.w, None, NOW)
        self.assertEqual(self.w.flags, [])
        fw.dxticker_eval(self.w, None, NOW)
        self.assertEqual(len(self.w.flags), 1)
        self.assertTrue(self.w.flags[0].startswith("warn:dxticker:dxticker health endpoint"))
        # recovery clears the streak
        fw.dxticker_eval(self.w, self.healthy(), NOW)
        self.w.flags = []
        fw.dxticker_eval(self.w, None, NOW)
        self.assertEqual(self.w.flags, [])

    def test_stale_feed_and_disconnect_debounced(self):
        stale = self.healthy(mqtt_connected=False, last_msg_age_s=2700)
        fw.dxticker_eval(self.w, stale, NOW)
        self.assertEqual(self.w.flags, [])
        fw.dxticker_eval(self.w, stale, NOW)
        self.assertEqual(self.w.flags,
                         ["warn:dxticker:dxticker MQTT disconnected, no spots for 45m "
                          "(2+ consecutive checks)"])

    def test_threshold_and_missing_age(self):
        fw.dxticker_eval(self.w, self.healthy(last_msg_age_s=1800), NOW)
        fw.dxticker_eval(self.w, self.healthy(last_msg_age_s=1800), NOW)
        self.assertEqual(self.w.flags, [])  # at the threshold is fine
        fw.dxticker_eval(self.w, self.healthy(last_msg_age_s=None), NOW)
        fw.dxticker_eval(self.w, self.healthy(last_msg_age_s=None), NOW)
        self.assertIn("no spots received since start", self.w.flags[0])

    def test_fetch_parses_json_and_survives_errors(self):
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"version": "0.2.0"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}/health"
        self.assertEqual(fw.dxticker_fetch(self.w, url), {"version": "0.2.0"})
        srv.server_close()
        self.assertIsNone(fw.dxticker_fetch(self.w, "http://127.0.0.1:9/health"))


class UpstreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.w = make_watcher(self.tmp.name, 'UPSTREAM_HOST="paul@192.168.1.119"\n')

    def tearDown(self):
        self.tmp.cleanup()

    def test_success_recent_is_silent(self):
        fw.upstream_eval(self.w, f"result=success\nlast={NOW - 3600}\n", NOW)
        self.assertEqual(self.w.flags, [])
        self.assertIn("- upstream: OK", self.w.triage_summary)

    def test_failed_result_flags(self):
        fw.upstream_eval(self.w, f"result=exit-code\nlast={NOW - 3600}\n", NOW)
        self.assertEqual(self.w.flags,
                         ["warn:upstream:upstream last run failed (Result=exit-code)"])

    def test_stuck_timer_flags_after_max_age(self):
        fw.upstream_eval(self.w, f"result=success\nlast={NOW - 5 * 3600}\n", NOW)
        self.assertEqual(self.w.flags, [])
        fw.upstream_eval(self.w, f"result=success\nlast={NOW - 7 * 3600}\n", NOW)
        self.assertEqual(self.w.flags,
                         ["warn:upstream:upstream timer last fired 7h ago (expected every 2h)"])

    def test_missing_units_debounced(self):
        fw.upstream_eval(self.w, "result=\nlast=0\n", NOW)
        self.assertEqual(self.w.flags, [])
        fw.upstream_eval(self.w, "result=\nlast=0\n", NOW)
        self.assertIn("timer or service not found", self.w.flags[0])

    def test_ssh_failure_is_report_only(self):
        self.w.run = StubRun([])
        fw.check_upstream(self.w, NOW)
        self.assertEqual(self.w.flags, [])
        self.assertTrue(any("coverage gap" in r for r in self.w.report))

    def test_probe_command_is_read_only(self):
        self.assertNotRegex(fw.UPSTREAM_PROBE, r"\b(start|stop|restart|enable|disable|rm)\b")
        self.assertIn("upstream.timer", fw.UPSTREAM_PROBE)

    def test_hours_ago_does_not_defeat_suppression(self):
        a = fw.normalize_dedup_key("WARN|- upstream timer last fired 7h ago")
        b = fw.normalize_dedup_key("WARN|- upstream timer last fired 9h ago")
        self.assertEqual(a, b)


class CoverageTests(unittest.TestCase):
    MACHINES = """# MACHINES rebuilt by findservers.sh
paul@192.168.1.85
paul@192.168.1.109
paul@192.168.1.119
paul@192.168.1.118
paul@192.168.1.130   # galaxy
paul@192.168.1.150
paul@192.168.1.171

paul@192.168.1.119
"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.w = make_watcher(
            self.tmp.name,
            'FLEET_HOSTS="paul@192.168.1.119 paul@192.168.1.118"\n'
            'MACHINES_SOURCE="paul@192.168.1.119:~/MACHINES"\n')

    def tearDown(self):
        self.tmp.cleanup()

    def stub(self, machines_rc=0):
        self.w.run = StubRun([
            ("hostname -I", (0, "192.168.1.109 192.168.1.177 2600::43\n")),
            ("cat ~/MACHINES", (machines_rc, self.MACHINES if machines_rc == 0
                                else "ssh: connect to host: No route to host")),
        ])

    def test_parse_machines(self):
        self.assertEqual(fw.parse_machines(self.MACHINES),
                         ["paul@192.168.1.85", "paul@192.168.1.109",
                          "paul@192.168.1.119", "paul@192.168.1.118",
                          "paul@192.168.1.130",
                          "paul@192.168.1.150", "paul@192.168.1.171"])

    def test_union_minus_self_peer_and_dupes(self):
        self.stub()
        hosts = fw.fleet_hosts(self.w, NOW)
        # FLEET_HOSTS first, then MACHINES; .109 is self, .171 is the peer.
        self.assertEqual(hosts, ["paul@192.168.1.119", "paul@192.168.1.118",
                                 "paul@192.168.1.85", "paul@192.168.1.130",
                                 "paul@192.168.1.150"])

    def test_exclusions_by_target_or_address(self):
        self.w.cfg["HOST_EXCLUDE"] = "paul@192.168.1.85 192.168.1.150"
        self.stub()
        self.assertEqual(fw.fleet_hosts(self.w, NOW),
                         ["paul@192.168.1.119", "paul@192.168.1.118",
                          "paul@192.168.1.130"])

    def test_cache_used_between_refreshes_and_on_failure(self):
        self.stub()
        fw.fleet_hosts(self.w, NOW)
        fetches = [c for c in self.w.run.calls if "cat ~/MACHINES" in " ".join(c)]
        self.assertEqual(len(fetches), 1)
        # Within 24 h: no new fetch.
        fw.fleet_hosts(self.w, NOW + 3600)
        fetches = [c for c in self.w.run.calls if "cat ~/MACHINES" in " ".join(c)]
        self.assertEqual(len(fetches), 1)
        # After 24 h the Vostro is down: the cached list still applies.
        self.stub(machines_rc=255)
        hosts = fw.fleet_hosts(self.w, NOW + 25 * 3600)
        self.assertIn("paul@192.168.1.130", hosts)
        self.assertTrue(any("using cache" in r for r in self.w.report))

    def test_no_cache_and_no_source_falls_back_to_fleet_hosts(self):
        self.stub(machines_rc=255)
        self.assertEqual(fw.fleet_hosts(self.w, NOW),
                         ["paul@192.168.1.119", "paul@192.168.1.118"])

    def test_login_refused_is_coverage_warning_not_unreachable(self):
        self.w.run = StubRun([
            ("paul@192.168.1.150", (255, "Host key verification failed.\n")),
        ])
        for _ in range(3):
            fw.check_host(self.w, "paul@192.168.1.150", "paul@192.168.1.150", NOW)
        kinds = {fw.flag_kind(f) for f in self.w.flags}
        self.assertEqual(kinds, {"coverage"})
        self.assertEqual(fw.worst_sev(self.w.flags), "WARN")
        self.assertIn("cannot log in", self.w.flags[0])

    def test_network_failure_still_crit_after_debounce(self):
        self.w.run = StubRun([])  # default: No route to host
        fw.check_host(self.w, "paul@192.168.1.130", "paul@192.168.1.130", NOW)
        self.assertEqual(self.w.flags, [])
        fw.check_host(self.w, "paul@192.168.1.130", "paul@192.168.1.130", NOW)
        self.assertEqual(fw.worst_sev(self.w.flags), "CRIT")

    def test_login_refused_markers(self):
        self.assertTrue(fw.login_refused("paul@x: Permission denied (publickey,password)."))
        self.assertTrue(fw.login_refused("Host key verification failed."))
        self.assertFalse(fw.login_refused("ssh: connect to host x port 22: No route to host"))
        self.assertFalse(fw.login_refused(""))


class TriageRoutingTests(unittest.TestCase):
    def test_service_groups_are_script_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = make_watcher(tmp)
            w.flags = ["warn:dxticker:dxticker MQTT disconnected",
                       "warn:upstream:upstream last run failed (Result=exit-code)",
                       "warn:coverage:paul@192.168.1.150 fleet-watch cannot log in",
                       "warn:failedsvc:paul@192.168.1.185 has 1 failed systemd service(s): logrotate"]
            self.assertTrue(fw.script_only_group(w, "dxticker"))
            self.assertTrue(fw.script_only_group(w, "upstream"))
            self.assertTrue(fw.script_only_group(w, "paul@192.168.1.150"))
            self.assertFalse(fw.script_only_group(w, "paul@192.168.1.185"))

    def test_new_hints_exist_and_are_read_only(self):
        for kind in ("dxticker", "upstream", "coverage"):
            hint = fw.hint_for(kind)
            self.assertTrue(hint.startswith("Check:"), kind)
            self.assertNotRegex(hint, r"\brm\b|delete|vacuum|truncate|mkfs|remove|restart")

    def test_run_skips_model_for_script_only_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = make_watcher(tmp, 'DXTICKER_HEALTH_URL="http://h/health"\n'
                                  'OLLAMA_HOST="http://192.168.1.236:11434"\n')
            model_calls, sent = [], []
            orig = (fw.check_host, fw.check_duels, fw.peer_check, fw.check_filelist,
                    fw.check_miners, fw.check_failed_units, fw.check_usenet_stall,
                    fw.dxticker_fetch, fw.call_model, fw.Watcher.run)
            try:
                for name in ("check_host", "check_duels", "peer_check", "check_filelist",
                             "check_miners", "check_failed_units", "check_usenet_stall"):
                    setattr(fw, name, lambda *a, **k: None)
                fw.dxticker_fetch = lambda w, url: None
                fw.call_model = lambda w, f: model_calls.append(f) or "prose"
                w.run = lambda cmd, timeout=60: (0, "")
                w.notify = lambda pri, title, msg, tags="": sent.append(msg) or True
                w.state.set("unreach_dxticker", 1)  # second consecutive miss
                fw.run(w)
            finally:
                (fw.check_host, fw.check_duels, fw.peer_check, fw.check_filelist,
                 fw.check_miners, fw.check_failed_units, fw.check_usenet_stall,
                 fw.dxticker_fetch, fw.call_model, fw.Watcher.run) = orig
            self.assertEqual(model_calls, [])
            self.assertEqual(len(sent), 1)
            self.assertIn("dxticker health endpoint not answering", sent[0])
            self.assertIn("journalctl --user -u dxticker", sent[0])


if __name__ == "__main__":
    unittest.main()


class TriageBudgetTests(unittest.TestCase):
    def test_budget_caps_model_time_and_falls_back_to_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = make_watcher(tmp, 'OLLAMA_HOST="http://192.168.1.236:11434"\n'
                                  'TRIAGE_BUDGET_SEC=100\n')
            clock = [1000.0]
            timeouts, sent = [], []

            def slow_model(w_, f, timeout=300):
                timeouts.append(timeout)
                clock[0] += 60           # every call eats 60 s
                return "prose"
            orig = (fw.check_host, fw.check_duels, fw.peer_check, fw.check_filelist,
                    fw.check_miners, fw.check_failed_units, fw.check_usenet_stall,
                    fw.check_dxticker, fw.check_upstream, fw.call_model, fw.time.monotonic)
            try:
                def add_flags(w_, host, label, now=None):
                    for h in ("a", "b", "c", "d"):
                        w_.flags.append(f"warn:reboot_required:host-{h} has a pending reboot")
                fw.check_host = lambda w_, host, label, now=None: (
                    add_flags(w_, host, label) if host == "localhost" else None)
                for name in ("check_duels", "peer_check", "check_filelist", "check_miners",
                             "check_failed_units", "check_usenet_stall",
                             "check_dxticker", "check_upstream"):
                    setattr(fw, name, lambda *a, **k: None)
                fw.call_model = slow_model
                fw.time.monotonic = lambda: clock[0]
                w.run = lambda cmd, timeout=60: (0, "")
                w.notify = lambda pri, title, msg, tags="": sent.append(msg) or True
                fw.run(w)
            finally:
                (fw.check_host, fw.check_duels, fw.peer_check, fw.check_filelist,
                 fw.check_miners, fw.check_failed_units, fw.check_usenet_stall,
                 fw.check_dxticker, fw.check_upstream, fw.call_model,
                 fw.time.monotonic) = orig
            # 100 s budget: call 1 (100 left), call 2 (40 left), then raw.
            self.assertEqual(timeouts, [100, 40])
            self.assertEqual(len(sent), 1)
            self.assertIn("host-c has a pending reboot", sent[0])   # raw finding
            self.assertIn("host-d has a pending reboot", sent[0])
            with open(w.log_file) as f:
                self.assertIn("triage budget spent", f.read())

    def test_default_budget_leaves_room_under_cron_limit(self):
        self.assertLessEqual(int(fw.DEFAULTS["TRIAGE_BUDGET_SEC"]), 300)


class MachinesTruthTests(unittest.TestCase):
    """MACHINES is the source of truth (Paul, 2026-11-21): conf host
    lists may only name machines that are in MACHINES. A stale entry
    (the retired Yoga at .230, the nonexistent .165) must be skipped,
    never probed."""

    MACHINES = "paul@192.168.1.119\npaul@192.168.1.171\n"

    def make(self, tmp, conf_text):
        w = make_watcher(
            tmp,
            'MACHINES_SOURCE="paul@192.168.1.119:~/MACHINES"\n' + conf_text)
        w.run = StubRun([
            ("hostname -I", (0, "192.168.1.109\n")),
            ("cat ~/MACHINES", (0, self.MACHINES)),
            ("systemctl", (0, "")),
        ])
        return w

    def test_fleet_hosts_drops_conf_host_not_in_machines(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = self.make(tmp, 'FLEET_HOSTS="paul@192.168.1.119 '
                               'paul@192.168.1.230"\n')
            hosts = fw.fleet_hosts(w, NOW)
            self.assertNotIn("paul@192.168.1.230", hosts)
            self.assertIn("paul@192.168.1.119", hosts)
            self.assertTrue(any("192.168.1.230" in r and "not in MACHINES" in r
                                for r in w.report))

    def test_failed_units_never_probes_phantoms(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = self.make(tmp, 'FAILED_UNIT_HOSTS="paul@192.168.1.119 '
                               'paul@192.168.1.230"\n')
            fw.check_failed_units(w, NOW)
            probed = " ".join(" ".join(c) for c in w.run.calls)
            self.assertIn("192.168.1.119", probed)
            self.assertNotIn("192.168.1.230", probed)

    def test_enforce_keeps_bare_ips_and_localhost(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = self.make(tmp, "")
            kept = fw.enforce_machines(
                w, ["192.168.1.119", "192.168.1.230", "localhost"], NOW,
                "FILELIST_HOSTS")
            self.assertEqual(kept, ["192.168.1.119", "localhost"])

    def test_no_roster_means_no_filtering(self):
        # MACHINES_SOURCE unset: no roster to enforce against, so conf
        # lists pass through untouched (no answer is not an empty list).
        with tempfile.TemporaryDirectory() as tmp:
            w = make_watcher(tmp, 'FAILED_UNIT_HOSTS="paul@h1 paul@h2"\n')
            w.run = StubRun([("systemctl", (0, ""))])
            fw.check_failed_units(w, NOW)
            probed = " ".join(" ".join(c) for c in w.run.calls)
            self.assertIn("paul@h1", probed)
            self.assertIn("paul@h2", probed)
