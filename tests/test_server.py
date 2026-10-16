"""Tests for the lens's auth handoff and its text surface.

Run: ~/dev/graphiti/mcp_server/.venv/bin/python3 -m pytest tests/ -q
"""
import importlib.util
import sys
import unittest
from pathlib import Path


def load():
    path = Path(__file__).resolve().parent.parent / "server.py"
    spec = importlib.util.spec_from_file_location("lens_server", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lens_server"] = mod
    spec.loader.exec_module(mod)
    return mod


class Nonces(unittest.TestCase):
    """The app hands the browser a nonce, never the long-lived token: a URL ends up in
    shell history, server logs and browser history, and the token outlives all three."""

    def setUp(self):
        self.m = load()

    def test_a_fresh_nonce_redeems(self):
        store = {}
        n = self.m.issue_nonce(store, now=100.0)
        self.assertTrue(self.m.redeem_nonce(store, n, now=100.5))

    def test_a_nonce_redeems_only_once(self):
        store = {}
        n = self.m.issue_nonce(store, now=100.0)
        self.assertTrue(self.m.redeem_nonce(store, n, now=100.5))
        self.assertFalse(self.m.redeem_nonce(store, n, now=100.6))

    def test_an_expired_nonce_is_refused(self):
        store = {}
        n = self.m.issue_nonce(store, now=100.0, ttl=30)
        self.assertFalse(self.m.redeem_nonce(store, n, now=131.0))

    def test_an_unknown_value_is_refused(self):
        self.assertFalse(self.m.redeem_nonce({}, "not-a-nonce", now=1.0))

    def test_empty_input_is_refused(self):
        store = {}
        self.m.issue_nonce(store, now=100.0)
        self.assertFalse(self.m.redeem_nonce(store, "", now=100.1))

    def test_issuing_prunes_what_has_expired(self):
        store = {}
        self.m.issue_nonce(store, now=100.0, ttl=30)
        self.m.issue_nonce(store, now=200.0, ttl=30)
        self.assertEqual(len(store), 1, "a stale nonce must not accumulate in memory")


class TokenFile(unittest.TestCase):
    """One home for the token. It used to live in the LaunchAgent plist, in this file, and
    in the server's own log — three copies, so rotating one left the others working."""

    def setUp(self):
        self.m = load()

    def test_it_writes_the_token_private_to_the_user(self):
        import stat, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".lens-token"
            self.m.write_token_file(path, "sekrit")
            self.assertEqual(path.read_text(), "sekrit")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_it_replaces_an_older_token(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".lens-token"
            path.write_text("old-and-longer")
            self.m.write_token_file(path, "new")
            self.assertEqual(path.read_text(), "new")

    def test_no_token_mode_removes_the_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".lens-token"
            path.write_text("stale")
            self.m.write_token_file(path, "")
            self.assertFalse(path.exists(), "a stale token file must not outlive --no-token")


class ExpiredLink(unittest.TestCase):
    """A copied or bookmarked lens URL stops working by design. A browser showing raw
    JSON for that looks like the tool is broken, which is how Alex first hit it."""

    def setUp(self):
        self.m = load()

    def test_it_explains_itself_in_a_browser(self):
        html = self.m.expired_page()
        self.assertIn("expired", html.lower())
        self.assertIn("Graphiti Lens", html, "it has to name the app that still works")

    def test_it_says_where_the_app_is(self):
        html = self.m.expired_page()
        self.assertTrue("Desktop" in html or "Applications" in html)

    def test_it_leaks_nothing(self):
        html = self.m.expired_page()
        for leak in ("lens_session", ".lens-token", "X-Lens-Token", "nonce="):
            self.assertNotIn(leak, html)


class ExplainText(unittest.TestCase):
    """The text surface. Every other view of this graph is pixels, which is no use to a
    screen reader, to an agent, or to anyone who does not think in pictures."""

    def setUp(self):
        self.m = load()
        self.payload = {
            "name": "Alex",
            "instances": [
                {"group": "global", "uuid": "aaaa1111", "facts": 40, "summary": "The operator."},
                {"group": "yapword", "uuid": "bbbb2222", "facts": 3, "summary": ""},
            ],
            "groups": ["global", "yapword"],
            "top_facts": [
                {"fact": "Alex built Yapword", "group": "yapword", "valid_at": "2026-06-01", "state": "live"},
                {"fact": "Alex used FalkorDB", "group": "global", "valid_at": "2026-05-02", "state": "superseded"},
            ],
            "recent_episodes": [
                {"name": "Sept sitrep", "created_at": "2026-09-10", "group": "global"},
            ],
            "superseded_recently": 1,
            "window_days": 7,
        }

    def test_it_names_the_entity_and_the_spread(self):
        text = self.m.format_explain(self.payload)
        self.assertIn("Alex", text)
        self.assertIn("2 groups", text)

    def test_superseded_facts_are_marked_in_words_not_colour(self):
        text = self.m.format_explain(self.payload)
        self.assertIn("superseded", text)
        self.assertNotIn("<", text, "the text surface must not emit markup")

    def test_it_reports_an_empty_result_plainly(self):
        text = self.m.format_explain({"name": "Nobody", "instances": [], "groups": [],
                                      "top_facts": [], "recent_episodes": [],
                                      "superseded_recently": 0, "window_days": 7})
        self.assertIn("Nobody", text)
        self.assertIn("nothing", text.lower())

    def test_embedded_newlines_do_not_shatter_the_layout(self):
        """Entity summaries in this graph really do contain newlines."""
        payload = dict(self.payload)
        payload["instances"] = [{"group": "global", "uuid": "aaaa1111", "facts": 40,
                                 "summary": "First line.\nSecond line.\nThird."}]
        payload["top_facts"] = [{"fact": "a fact\nwith a break", "group": "global",
                                 "valid_at": "2026-06-01", "state": "live"}]
        text = self.m.format_explain(payload)
        body = [l for l in text.splitlines() if l.startswith("  ")]
        self.assertTrue(all("Second line" not in l or "First line" in l for l in body))
        self.assertEqual(len([l for l in body if "First line" in l]), 1,
                         "a multi-line summary must occupy exactly one row")
        self.assertEqual(len([l for l in body if "with a break" in l]), 1)

    def test_every_line_is_plain_text(self):
        for line in self.m.format_explain(self.payload).splitlines():
            self.assertNotIn("\t", line, "tabs break screen-reader and terminal alignment")


if __name__ == "__main__":
    unittest.main()


class TheProvenanceTierIsReadFromTheNodeFirst(unittest.TestCase):
    """2026-09-15: the MCP server stores the tier on the Episodic node; a prefix-only count
    read 7.7% critical while 98.9% of the week's writes carried a tier."""

    def test_the_expression_reads_the_stored_tier_before_any_prefix(self):
        mod = load()
        self.assertTrue(mod.TIER_EXPR.startswith("coalesce(e.tier"))
        for prefix in ("OBSERVED:", "DERIVED:", "VERIFIED:", "DECIDED:"):
            self.assertIn(prefix, mod.TIER_EXPR)

    def test_no_query_keeps_a_prefix_check_of_its_own(self):
        src = (Path(__file__).resolve().parent.parent / "server.py").read_text()
        self.assertEqual(src.count("STARTS WITH 'OBSERVED:'"), 1, "only TIER_EXPR may test the prefix")


class TheHealthFileIsTheAuditOnDisk(unittest.TestCase):
    """D-mem-010 (2026-10-13): the audit is written to lens-health.json so readers need
    neither the bearer token nor a live Lens. The UpCloud daemon's get_status reads a
    pushed copy of this file; before it, every call said `reachable: false` from the host."""

    def setUp(self):
        import tempfile
        self.m = load()
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "nested" / "lens-health.json"

    def test_it_writes_valid_json_and_creates_the_folder(self):
        out = self.m.write_health_file(self.path, {"generated_at": "2026-10-13T00:00:00+00:00",
                                                   "checks": [{"id": "dedup", "status": "good"}]})
        import json
        data = json.loads(out.read_text())
        self.assertEqual(data["generated_at"], "2026-10-13T00:00:00+00:00")
        self.assertEqual(data["checks"][0]["id"], "dedup")

    def test_it_leaves_no_temp_file_behind(self):
        self.m.write_health_file(self.path, {"generated_at": "x"})
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["lens-health.json"])

    def test_it_coerces_neo4j_temporals_like_the_http_surface_does(self):
        class Stamp:
            def isoformat(self):
                return "2026-10-13T01:02:03+00:00"
        self.m.write_health_file(self.path, {"last": Stamp()})
        import json
        self.assertEqual(json.loads(self.path.read_text())["last"], "2026-10-13T01:02:03+00:00")

    def test_the_loop_survives_a_failed_audit_and_keeps_the_last_good_file(self):
        import threading
        calls = []
        stop = threading.Event()

        def fake_api_health(graph, q):
            calls.append(1)
            if len(calls) == 1:
                return {"generated_at": "first"}
            if len(calls) == 2:
                raise RuntimeError("tunnel asleep")
            stop.set()
            return {"generated_at": "third"}

        logged = []
        self.m.api_health = fake_api_health
        self.m.health_file_loop(None, self.path, 0, stop, log=logged.append)
        import json
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(logged), 1)
        self.assertIn("tunnel asleep", logged[0])
        self.assertEqual(json.loads(self.path.read_text())["generated_at"], "third")

    def test_the_loop_stops_when_told(self):
        import threading
        stop = threading.Event()
        stop.set()
        self.m.api_health = lambda g, q: self.fail("must not audit once stopped")
        self.m.health_file_loop(None, self.path, 0, stop)
        self.assertFalse(self.path.exists())

    def test_the_flag_turns_it_off(self):
        src = (Path(__file__).resolve().parent.parent / "server.py").read_text()
        self.assertIn('"--no-health-file"', src)
        self.assertIn("if not args.no_health_file:", src)


class FakeGraph:
    """Answers api_health's queries by a distinctive substring of the Cypher, in order.
    Anything unmatched returns no rows, which api_health reads as zeros. Records every
    (cypher, params) so a test can assert on what was asked, not only on what came back."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.calls = []

    def read(self, cypher, params=None, timeout=None):
        self.calls.append((cypher, params or {}))
        for needle, rows in self.answers:
            if needle in cypher:
                return rows
        return []

    def one(self, cypher, params=None, default=None):
        rows = self.read(cypher, params)
        return rows[0] if rows else (default if default is not None else {})


def audit(answers, group=None):
    """Run api_health against a FakeGraph; returns (payload, graph, checks by id)."""
    import os, tempfile
    os.environ["GRAPHITI_SPOOL_DIR"] = tempfile.mkdtemp()
    mod = load()
    g = FakeGraph(answers)
    out = mod.api_health(g, {"group": [group]} if group else {})
    return out, g, {c["id"]: c for c in out["checks"]}


EVIDENCE_N = "MATCH (v:Evidence)"
LIVE_TRUST = "r.trust_state IN ['current', 'stale', 'unverified']"
STALE_REASONS = "UNWIND coalesce(r.stale_reasons"
OBSERVED_14D = "duration({days: 14})"


class StaleFactsChecks(unittest.TestCase):
    """2026-10-15: two checks over the (:Evidence) layer. Until a database has Evidence they
    must read 'not measured', not a false critical; 'stale' means a fact's evidence moved."""

    def test_no_evidence_means_not_measured_not_critical(self):
        _, g, checks = audit([(EVIDENCE_N, [{"c": 0}])])
        for cid in ("evidence_fresh", "anchor_coverage"):
            self.assertIsNone(checks[cid]["value"])
            self.assertEqual(checks[cid]["status"], "not_measured")
            self.assertIn("no evidence links recorded yet", checks[cid]["detail"])
        asked = [c for c, _ in g.calls]
        self.assertFalse(any(LIVE_TRUST in c or OBSERVED_14D in c for c in asked),
                         "with no Evidence there is nothing to ask the fact edges")

    def test_not_measured_does_not_touch_the_other_checks(self):
        out, _, checks = audit([(EVIDENCE_N, [{"c": 0}])])
        self.assertEqual(len(out["checks"]), 13)
        self.assertEqual([c["id"] for c in out["checks"] if c["status"] == "not_measured"],
                         ["evidence_fresh", "anchor_coverage"])
        self.assertNotIn("not_measured", {c["status"] for c in out["checks"]
                                          if c["id"] not in ("evidence_fresh", "anchor_coverage")})

    def test_health_file_carries_not_measured_as_json_null(self):
        import json, tempfile
        out, _, _ = audit([(EVIDENCE_N, [{"c": 0}])])
        path = Path(tempfile.mkdtemp()) / "lens-health.json"
        load().write_health_file(path, out)
        data = {c["id"]: c for c in json.loads(path.read_text())["checks"]}
        self.assertIsNone(data["evidence_fresh"]["value"])
        self.assertEqual(data["evidence_fresh"]["status"], "not_measured")

    def test_evidence_fresh_is_current_over_current_plus_stale(self):
        _, _, checks = audit([
            (EVIDENCE_N, [{"c": 5}]),
            (STALE_REASONS, [{"reason": "git:abouchard11/x@main:src/a.py#L1-L9 changed"},
                             {"reason": "host:example.com/p moved"}]),
            (LIVE_TRUST, [{"current": 18, "stale": 2}]),
        ])
        c = checks["evidence_fresh"]
        self.assertEqual(c["value"], 90.0)
        self.assertEqual(c["status"], "warning")
        self.assertTrue(c["detail"].startswith("2 stale: git:abouchard11/x@main:src/a.py#L1-L9 changed"))
        self.assertIn("host:example.com/p moved", c["detail"])

    def test_unverified_facts_are_shown_but_never_move_the_percentage(self):
        _, _, checks = audit([(EVIDENCE_N, [{"c": 3}]),
                              (LIVE_TRUST, [{"current": 9, "stale": 1, "unverified": 7}])])
        c = checks["evidence_fresh"]
        self.assertEqual(c["value"], 90.0)
        self.assertIn("7 unverified", c["detail"])
        _, _, checks = audit([(EVIDENCE_N, [{"c": 3}]),
                              (LIVE_TRUST, [{"current": 0, "stale": 0, "unverified": 4}])])
        self.assertIsNone(checks["evidence_fresh"]["value"])
        self.assertIn("4 unverified", checks["evidence_fresh"]["detail"])

    def test_evidence_fresh_thresholds(self):
        for current, stale, status in ((95, 5, "good"), (80, 20, "warning"), (79, 21, "critical")):
            _, _, checks = audit([(EVIDENCE_N, [{"c": 1}]),
                                  (LIVE_TRUST, [{"current": current, "stale": stale}])])
            self.assertEqual(checks["evidence_fresh"]["status"], status, (current, stale))

    def test_facts_without_a_trust_state_are_outside_the_denominator(self):
        _, g, checks = audit([(EVIDENCE_N, [{"c": 1}]),
                              (LIVE_TRUST, [{"current": 3, "stale": 0}])])
        # 3 anchored facts, all current: 100%, however many unanchored facts the graph holds.
        self.assertEqual(checks["evidence_fresh"]["value"], 100.0)
        self.assertIn("none stale", checks["evidence_fresh"]["detail"])
        q = [c for c, _ in g.calls if LIVE_TRUST in c][0]
        self.assertIn("r.trust_state IN ['current', 'stale', 'unverified']", q)
        self.assertNotIn("IS NULL OR r.trust_state", q)

    def test_evidence_but_no_checked_fact_is_still_not_measured(self):
        _, _, checks = audit([(EVIDENCE_N, [{"c": 4}]), (LIVE_TRUST, [{"current": 0, "stale": 0}])])
        self.assertEqual(checks["evidence_fresh"]["status"], "not_measured")
        self.assertIn("no live fact has been checked", checks["evidence_fresh"]["detail"])

    def test_evidence_fresh_counts_only_live_edges(self):
        mod = load()
        _, g, _ = audit([(EVIDENCE_N, [{"c": 1}]), (LIVE_TRUST, [{"current": 1, "stale": 0}])])
        q, params = [(c, p) for c, p in g.calls if LIVE_TRUST in c][0]
        self.assertIn(mod.LIVE_PREDICATE, q, "same liveness as the Explore view")
        self.assertIn("asof", params)

    def test_anchor_coverage_math_and_thresholds(self):
        for total, cited, value, status in ((10, 6, 60.0, "good"), (10, 3, 30.0, "warning"),
                                            (10, 1, 10.0, "critical")):
            _, _, checks = audit([(EVIDENCE_N, [{"c": 1}]),
                                  (OBSERVED_14D, [{"total": total, "cited": cited}])])
            c = checks["anchor_coverage"]
            self.assertEqual((c["value"], c["status"]), (value, status))
            self.assertIn(f"{cited} of {total} observed", c["detail"])

    def test_anchor_coverage_only_looks_at_observed_episodes_via_the_stored_tier(self):
        mod = load()
        _, g, _ = audit([(EVIDENCE_N, [{"c": 1}]), (OBSERVED_14D, [{"total": 1, "cited": 1}])])
        q = [c for c, _ in g.calls if OBSERVED_14D in c][0]
        self.assertIn(f"{mod.TIER_EXPR} = 'observed'", q)
        self.assertIn("[:CITES]->(:Evidence)", q)

    def test_anchor_coverage_with_no_recent_observed_writes_is_not_measured(self):
        _, _, checks = audit([(EVIDENCE_N, [{"c": 1}]), (OBSERVED_14D, [{"total": 0, "cited": 0}])])
        self.assertEqual(checks["anchor_coverage"]["status"], "not_measured")
        self.assertIn("no observed writes", checks["anchor_coverage"]["detail"])

    def test_the_group_filter_reaches_both_checks(self):
        _, g, _ = audit([(EVIDENCE_N, [{"c": 1}]),
                         (LIVE_TRUST, [{"current": 1, "stale": 0}]),
                         (OBSERVED_14D, [{"total": 1, "cited": 1}])], group="buylandfl")
        for needle in (EVIDENCE_N, LIVE_TRUST, OBSERVED_14D):
            q, params = [(c, p) for c, p in g.calls if needle in c][0]
            self.assertIn("$group IS NULL OR", q)
            self.assertEqual(params["group"], "buylandfl")

    def test_status_helper_keeps_the_old_thresholds(self):
        m = load()
        c = {"good": 95, "warn": 80}
        self.assertEqual(m.check_status({**c, "value": 95}), "good")
        self.assertEqual(m.check_status({**c, "value": 80}), "warning")
        self.assertEqual(m.check_status({**c, "value": 79.9}), "critical")
        self.assertEqual(m.check_status({**c, "value": 0.0}), "critical")
        self.assertEqual(m.check_status({**c, "value": None}), "not_measured")


class QuietGroupsAreNotStaleFacts(unittest.TestCase):
    """2026-10-15: the groups-by-last-write list was called 'stale'. Stale now means a fact
    whose evidence changed; a group that stopped writing is quiet."""

    ROWS = [{"group": "yapword", "last": "2026-09-01", "episodes": 12},
            {"group": "global", "last": "2026-10-14", "episodes": 400}]

    def test_quiet_groups_is_the_key_and_the_old_key_is_a_one_release_alias(self):
        out, _, _ = audit([("AS last, count(*) AS episodes", self.ROWS), (EVIDENCE_N, [{"c": 0}])])
        self.assertEqual([r["group"] for r in out["quiet_groups"]], ["yapword", "global"])
        self.assertEqual(out["group_staleness"], out["quiet_groups"])

    def test_the_ui_reads_the_new_key_and_says_quiet(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text()
        self.assertIn("h.quiet_groups", js)
        self.assertNotIn("gone stale", js)
