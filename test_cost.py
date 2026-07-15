import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import cost


class HermesHarnessTests(unittest.TestCase):
    def _make_db(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT,
                model TEXT,
                started_at REAL,
                ended_at REAL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_write_tokens INTEGER,
                reasoning_tokens INTEGER,
                cwd TEXT,
                billing_provider TEXT,
                billing_base_url TEXT,
                billing_mode TEXT,
                estimated_cost_usd REAL,
                actual_cost_usd REAL,
                cost_status TEXT,
                cost_source TEXT,
                api_call_count INTEGER,
                parent_session_id TEXT,
                end_reason TEXT
            );
            CREATE TABLE session_model_usage (
                session_id TEXT,
                model TEXT,
                billing_provider TEXT,
                billing_base_url TEXT,
                billing_mode TEXT,
                api_call_count INTEGER,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_write_tokens INTEGER,
                reasoning_tokens INTEGER,
                estimated_cost_usd REAL,
                actual_cost_usd REAL,
                cost_status TEXT,
                cost_source TEXT,
                first_seen REAL,
                last_seen REAL
            );
            CREATE TABLE messages (
                session_id TEXT,
                content TEXT
            );
            """
        )
        conn.executemany(
            """INSERT INTO sessions VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )""",
            [
                (
                    "session-one", "cli", "openai/gpt-test", 1000.0, 1200.0,
                    100, 50, 10, 2, 7, "/work/one", "openai", "", "api",
                    0.0, 0.0, "estimated", "catalog", 2, None, "completed",
                ),
                (
                    "session-two", "cron", "claude-test", 2000.0, None,
                    25, 5, 0, 0, 3, "/work/two", "anthropic", "", "api",
                    0.0, 0.0, "estimated", "catalog", 1, None, None,
                ),
                (
                    "compression-parent", "cli", "openai/gpt-test", 400.0, 500.0,
                    10, 2, 0, 0, 0, "/work/compressed", "openai", "", "api",
                    0.0, 0.0, "estimated", "catalog", 1, None, "compression",
                ),
                (
                    "compression-child", "cli", "openai/gpt-test", 600.0, 700.0,
                    20, 3, 0, 0, 0, "/work/compressed", "openai", "", "api",
                    0.0, 0.0, "estimated", "catalog", 1,
                    "compression-parent", "completed",
                ),
                (
                    "ordinary-parent", "cli", "openai/gpt-test", 300.0, 350.0,
                    7, 1, 0, 0, 0, "/work/branch", "openai", "", "api",
                    0.0, 0.0, "estimated", "catalog", 1, None, "completed",
                ),
                (
                    "branch-child", "cli", "openai/gpt-test", 800.0, 900.0,
                    11, 2, 0, 0, 0, "/work/branch", "openai", "", "api",
                    0.0, 0.0, "estimated", "catalog", 1,
                    "ordinary-parent", "completed",
                ),
            ],
        )
        conn.execute(
            """INSERT INTO session_model_usage VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )""",
            (
                "session-one", "openai/gpt-test", "openai", "", "api", 1,
                60, 30, 0, 0, 4, 0.0, 0.0, "estimated", "catalog",
                1000.0, 1100.0,
            ),
        )
        conn.execute(
            "INSERT INTO messages VALUES (?, ?)",
            ("session-one", "private transcript text must never be read"),
        )
        conn.commit()
        conn.close()

    def test_load_hermes_db_uses_model_usage_and_reconciles_residuals(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "state.db"
            self._make_db(db)

            sessions = cost.load_hermes_db(str(db))

        self.assertEqual(
            [s.session_id for s in sessions[:2]], ["session-two", "session-one"]
        )
        first = next(s for s in sessions if s.session_id == "session-one")
        self.assertEqual(first.cwd, "/work/one")
        self.assertEqual(first.first_ts, "1970-01-01T00:16:40Z")
        self.assertEqual(first.last_ts, "1970-01-01T00:20:00Z")
        self.assertEqual(first.by_model["openai/gpt-test"].input, 100)
        self.assertEqual(first.by_model["openai/gpt-test"].output, 50)
        self.assertEqual(first.by_model["openai/gpt-test"].cache_read, 10)
        self.assertEqual(first.by_model["openai/gpt-test"].cache_write, 2)
        self.assertNotIn("private transcript", repr(sessions))

        second = next(s for s in sessions if s.session_id == "session-two")
        self.assertEqual(second.by_model["claude-test"].input, 25)
        self.assertEqual(second.last_ts, "1970-01-01T00:33:20Z")

    def test_discover_hermes_reads_only_the_active_home(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            default_db = home / ".hermes" / "state.db"
            profile_db = home / ".hermes" / "profiles" / "work" / "state.db"
            default_db.parent.mkdir(parents=True)
            profile_db.parent.mkdir(parents=True)
            default_db.touch()
            profile_db.touch()

            with mock.patch.object(Path, "home", return_value=home), mock.patch.dict(
                os.environ, {"HERMES_HOME": str(profile_db.parent)}, clear=False
            ):
                found = cost.discover("hermes")

        self.assertEqual(found, [str(profile_db)])

    def test_hermes_dotted_claude_model_normalizes_for_pricing(self):
        price = {"input": 1.0, "output": 2.0}
        prices = {"claude-opus-4-8": price}
        self.assertIs(cost.price_for("anthropic/claude-opus-4.8", prices), price)

    def test_load_sessions_dispatches_sqlite_without_jsonl_parsing(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "state.db"
            self._make_db(db)
            loaded = cost.load_sessions(str(db), "hermes")
        self.assertEqual(len(loaded), 5)
        self.assertTrue(all(s.harness == "hermes" for s in loaded))

    def test_only_compression_lineage_is_merged(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "state.db"
            self._make_db(db)
            loaded = cost.load_hermes_db(str(db))

        by_id = {s.session_id: s for s in loaded}
        self.assertNotIn("compression-parent", by_id)
        compressed = by_id["compression-child"].combined()
        self.assertEqual((compressed.input, compressed.output), (30, 5))
        self.assertIn("ordinary-parent", by_id)
        self.assertIn("branch-child", by_id)

    def test_default_current_selects_newest_logical_hermes_session(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "state.db"
            self._make_db(db)
            output = StringIO()
            prices = {
                "openai/gpt-test": {"input": 0.0, "output": 0.0},
                "claude-test": {"input": 0.0, "output": 0.0},
                "_meta": {},
            }
            with mock.patch.object(cost, "discover", return_value=[str(db)]), mock.patch.object(
                cost, "load_prices", return_value=prices
            ), redirect_stdout(output):
                rc = cost.main(["--harness", "hermes", "--json", "--offline"])

        self.assertEqual(rc, 0)
        payload = __import__("json").loads(output.getvalue())
        self.assertEqual(
            [s["session_id"] for s in payload["sessions"]], ["session-two"]
        )

    def test_current_jsonl_uses_session_activity_not_file_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            stale = root / "stale.jsonl"
            latest = root / "latest.jsonl"
            stale.write_text(
                '{"timestamp":"2026-01-01T00:00:00Z","session_id":"stale-session","model":"gpt-5","usage":{"input_tokens":1,"output_tokens":1}}\n'
            )
            latest.write_text(
                '{"timestamp":"2026-07-13T00:00:00Z","session_id":"latest-session","model":"gpt-5","usage":{"input_tokens":2,"output_tokens":2}}\n'
            )
            os.utime(stale, (2_000_000_000, 2_000_000_000))
            os.utime(latest, (1_000_000_000, 1_000_000_000))
            output = StringIO()
            with mock.patch.object(
                cost, "discover", return_value=[str(stale), str(latest)]
            ), redirect_stdout(output):
                rc = cost.main(
                    ["--current", "--harness", "codex", "--json", "--offline"]
                )

        self.assertEqual(rc, 0)
        payload = __import__("json").loads(output.getvalue())
        self.assertEqual(payload["sessions"][0]["session_id"], "latest-session")


if __name__ == "__main__":
    unittest.main()
