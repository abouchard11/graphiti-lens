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
