import hashlib
import json
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



class RunReportTests(unittest.TestCase):
    """--run-report reads one Claude Code main session plus its subagent transcripts.
    Every usage call is counted once across all files, roles come only from the
    model family or an explicit role map, and USD is a public-rate equivalent."""

    SID = "11111111-2222-3333-4444-555555555555"
    AID = "aaaa00000000bbbb1"      # haiku subagent, mapped as builder
    BID = "aaaa00000000cccc2"      # haiku subagent, mapped as verifier
    PRICES = {
        "claude-sonnet-5-5": {"input": 2e-6, "output": 1e-5, "cache_read": 1e-7,
                              "cache_write": 2.5e-6, "cache_write_1h": 4e-6},
        "claude-haiku-5-5": {"input": 1e-6, "output": 5e-6, "cache_read": 1e-8,
                             "cache_write": 1.25e-6, "cache_write_1h": 2e-6},
        "_meta": {"source": "test", "source_url": "https://example.invalid/prices",
                  "fetched_at": "2026-01-01T00:00:00Z", "model_count": 2},
    }
    USAGE = {"input_tokens": 10, "output_tokens": 100,
             "cache_read_input_tokens": 1_000,
             "cache_creation": {"ephemeral_5m_input_tokens": 50,
                                "ephemeral_1h_input_tokens": 0}}

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.proj = self.home / ".claude" / "projects" / "-work-repo"
        self.proj.mkdir(parents=True)
        self.sub_dir = self.proj / self.SID / "subagents"
        self.sub_dir.mkdir(parents=True)
        self._env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self._env.start()
        self._prices = mock.patch.object(cost, "load_prices", return_value=self.PRICES)
        self._prices.start()

    def tearDown(self) -> None:
        self._prices.stop()
        self._env.stop()
        self._tmp.cleanup()

    # --- fixtures -------------------------------------------------------------

    def _rec(self, msg_id, req, model, usage=None, sidechain=False, uuid="u", advisor=None):
        d = {"type": "assistant", "uuid": uuid, "requestId": req,
             "sessionId": self.SID, "cwd": "/work/repo", "isSidechain": sidechain,
             "timestamp": "2026-01-01T00:00:01.000Z",
             "message": {"id": msg_id, "model": model, "usage": usage or self.USAGE}}
        if advisor:
            d["advisorModel"] = advisor
        return json.dumps(d)

    def _write(self, path: Path, lines: list[str]) -> None:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _main_file(self, lines: list[str]) -> Path:
        p = self.proj / f"{self.SID}.jsonl"
        self._write(p, lines)
        return p

    def _sub_file(self, agent_id: str, lines: list[str], meta: bool = True) -> Path:
        p = self.sub_dir / f"agent-{agent_id}.jsonl"
        self._write(p, lines)
        if meta:
            (self.sub_dir / f"agent-{agent_id}.meta.json").write_text(
                json.dumps({"agentType": "general-purpose", "model": "haiku"}), encoding="utf-8")
        return p

    def _run(self, *extra: str) -> tuple[int, dict | None, str]:
        buf = StringIO()
        err = StringIO()
        with redirect_stdout(buf), mock.patch("sys.stderr", err):
            code = cost.main(["--run-report", "--offline", *extra])
        out = buf.getvalue()
        try:
            data = json.loads(out) if "--json" in extra or "--markdown" not in extra else None
        except ValueError:
            data = None
        return code, data, out + err.getvalue()

    def _rows(self, rep: dict) -> dict:
        return {(r["role"], r["model"]): r for r in rep["rows"]}

    def _standard_fixture(self) -> None:
        # Main session: lead (sonnet) with a streamed call written twice, plus one call
        # that is also present in a subagent transcript (must count once).
        self._main_file([
            self._rec("msg_m1", "req_m1", "claude-sonnet-5-5", uuid="a"),
            self._rec("msg_m1", "req_m1", "claude-sonnet-5-5", uuid="b"),
            self._rec("msg_m2", "req_m2", "claude-sonnet-5-5", uuid="c"),
            self._rec("msg_shared", "req_shared", "claude-haiku-5-5", uuid="d", sidechain=False),
        ])
        self._sub_file(self.AID, [
            self._rec("msg_s1", "req_s1", "claude-haiku-5-5", sidechain=True, uuid="e"),
            self._rec("msg_shared", "req_shared", "claude-haiku-5-5", sidechain=True, uuid="f"),
        ])
        self._sub_file(self.BID, [
            self._rec("msg_s2", "req_s2", "claude-haiku-5-5", sidechain=True, uuid="g"),
        ])

    # --- tests ----------------------------------------------------------------

    def test_duplicate_calls_across_main_and_subagent_count_once(self) -> None:
        self._standard_fixture()
        code, rep, _ = self._run("--json")
        self.assertEqual(code, 0)
        # 4 main lines + 2 + 1 subagent lines = 7 keyed records, 5 distinct API calls
        # (msg_m1 written twice in main; msg_shared in main and a subagent) -> 2 ignored.
        self.assertEqual(rep["dedupe"]["keyedRecords"], 7)
        self.assertEqual(rep["dedupe"]["duplicatesIgnored"], 2)
        # Each call contributes 10 input, 100 output, 1000 cache read, 50 cache write.
        self.assertEqual(rep["measuredTokens"]["input"], 5 * 10)
        self.assertEqual(rep["measuredTokens"]["output"], 5 * 100)
        self.assertEqual(rep["measuredTokens"]["cacheRead"], 5 * 1_000)
        self.assertEqual(rep["measuredTokens"]["cacheWrite"], 5 * 50)

    def test_default_roles_are_lead_and_subagent_family_never_builder_or_verifier(self) -> None:
        self._standard_fixture()
        _, rep, _ = self._run("--json")
        rows = self._rows(rep)
        self.assertIn(("lead", "claude-sonnet-5-5"), rows)
        self.assertEqual(rep["session"]["leadModel"], "claude-sonnet-5-5")
        self.assertIn(("subagent:claude-haiku-5-5", "claude-haiku-5-5"), rows)
        self.assertFalse(any(r["role"] in ("builder", "verifier") for r in rep["rows"]))

    def test_role_map_assigns_builder_and_verifier_explicitly(self) -> None:
        self._standard_fixture()
        mapping = self.home / "roles.json"
        mapping.write_text(json.dumps({
            "_comment": "ignored",
            self.AID: "builder",
            f"agent-{self.BID}": "verifier",
        }), encoding="utf-8")
        _, rep, _ = self._run("--json", "--role-map", str(mapping))
        self.assertTrue(rep["roleMap"]["provided"])
        self.assertEqual(rep["roleMap"]["entries"], 2)
        self.assertEqual(rep["complete"], True)
        roles = set(rep["byRole"])
        self.assertTrue({"builder", "verifier", "lead"} <= roles)
        self.assertNotIn("subagent:claude-haiku-5-5", roles)

    def test_bad_role_map_is_a_usage_error(self) -> None:
        self._standard_fixture()
        bad = self.home / "bad.json"
        bad.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
        code, _, text = self._run("--json", "--role-map", str(bad))
        self.assertEqual(code, 2)
        self.assertIn("role map", text)

    def test_unknown_model_is_unpriced_and_never_substituted(self) -> None:
        self._main_file([
            self._rec("msg_m1", "req_m1", "claude-sonnet-5-5"),
            # A model with no catalog entry, sharing a name fragment with a priced model.
            self._rec("msg_x", "req_x", "claude-haiku-5-9-preview"),
        ])
        _, rep, _ = self._run("--json")
        unknown = {u["model"]: u for u in rep["unknownPrices"]}
        self.assertIn("claude-haiku-5-9-preview", unknown)
        self.assertEqual(unknown["claude-haiku-5-9-preview"]["tokens"], 1_110 + 50)
        self.assertEqual(unknown["claude-haiku-5-9-preview"]["reason"], "no-catalog-entry")
        self.assertFalse(rep["publicRateUsdEquivalent"]["complete"])
        row = self._rows(rep)[("unattributed", "claude-haiku-5-9-preview")]
        self.assertFalse(row["priced"])
        self.assertIsNone(row["publicRateUsdEquivalent"])
        # Only the sonnet call contributes USD.
        sonnet = cost.Bucket(input=10, output=100, cache_read=1_000, cache_write=50)
        self.assertAlmostEqual(rep["publicRateUsdEquivalent"]["usd"],
                               sonnet.cost("claude-sonnet-5-5", self.PRICES))

    def test_role_map_key_with_no_transcript_marks_report_incomplete(self) -> None:
        self._standard_fixture()
        mapping = self.home / "roles.json"
        mapping.write_text(json.dumps({"deadbeefdeadbeef0": "verifier"}), encoding="utf-8")
        code, rep, _ = self._run("--json", "--role-map", str(mapping))
        self.assertEqual(code, 0)
        self.assertFalse(rep["complete"])
        self.assertTrue(any(i["id"] == "deadbeefdeadbeef0" for i in rep["inaccessible"]))
        self.assertTrue(rep["incompleteReasons"])

    def test_strict_exits_3_when_incomplete(self) -> None:
        self._standard_fixture()
        mapping = self.home / "roles.json"
        mapping.write_text(json.dumps({"deadbeefdeadbeef0": "verifier"}), encoding="utf-8")
        code, _, _ = self._run("--json", "--role-map", str(mapping), "--strict")
        self.assertEqual(code, 3)

    def test_meta_without_transcript_is_inaccessible(self) -> None:
        self._standard_fixture()
        (self.sub_dir / "agent-ccccdddd00000001.meta.json").write_text(
            json.dumps({"agentType": "general-purpose"}), encoding="utf-8")
        _, rep, _ = self._run("--json")
        self.assertFalse(rep["complete"])
        self.assertTrue(any(i["id"] == "ccccdddd00000001" and i["kind"] == "subagent"
                            for i in rep["inaccessible"]))

    def test_unparsable_line_marks_report_incomplete(self) -> None:
        p = self._main_file([self._rec("msg_m1", "req_m1", "claude-sonnet-5-5")])
        p.write_text(p.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")
        _, rep, _ = self._run("--json")
        self.assertFalse(rep["complete"])
        self.assertTrue(any("unparsable" in r for r in rep["incompleteReasons"]))

    def test_billed_spend_is_null_unless_supplied(self) -> None:
        self._standard_fixture()
        _, rep, _ = self._run("--json")
        self.assertIsNone(rep["actualBilledSpend"]["usd"])
        self.assertEqual(rep["actualBilledSpend"]["reason"], "not measured by this tool")

    def test_billed_spend_is_echoed_with_provenance(self) -> None:
        self._standard_fixture()
        _, rep, _ = self._run("--json", "--billed-usd", "12.5",
                              "--billed-source", "Anthropic invoice 2026-10")
        self.assertEqual(rep["actualBilledSpend"]["usd"], 12.5)
        self.assertEqual(rep["actualBilledSpend"]["source"], "Anthropic invoice 2026-10")
        self.assertIn("user-supplied", rep["actualBilledSpend"]["provenance"])
        # The public-rate figure is untouched by the billed figure.
        self.assertNotEqual(rep["publicRateUsdEquivalent"]["usd"], 12.5)

    def test_billed_usd_without_source_is_rejected(self) -> None:
        self._standard_fixture()
        code, _, text = self._run("--json", "--billed-usd", "3")
        self.assertEqual(code, 2)
        self.assertIn("together", text)

    def test_json_schema_keys_are_stable(self) -> None:
        self._standard_fixture()
        _, rep, _ = self._run("--json")
        self.assertEqual(rep["schema"], "skill-cost-run-report-v1")
        self.assertEqual(rep["tool"]["version"], cost.TOOL_VERSION)
        for key in ("generatedAt", "complete", "incompleteReasons", "session", "files",
                    "inaccessible", "roleMap", "dedupe", "measuredTokens",
                    "publicRateUsdEquivalent", "unknownPrices", "byRole", "rows",
                    "actualBilledSpend", "disclaimer", "prices", "warnings",
                    "malformedUsage", "priceAssumptions"):
            self.assertIn(key, rep)
        for key in ("sha256", "sourceUrl", "fetchedAt", "modelCount", "file"):
            self.assertIn(key, rep["prices"])
        self.assertEqual(rep["publicRateUsdEquivalent"]["kind"],
                         "public-rate equivalent, not a bill")
        files = {f["path"].rsplit("/", 1)[-1]: f for f in rep["files"]}
        self.assertIn(f"{self.SID}.jsonl", files)
        self.assertIn(f"agent-{self.AID}.jsonl", files)
        for f in rep["files"]:
            self.assertFalse(os.path.isabs(f["path"]))
            self.assertEqual(len(f["sha256"]), 64)

    def test_markdown_rendering_has_table_and_disclaimer(self) -> None:
        self._standard_fixture()
        _, _, text = self._run("--markdown")
        self.assertIn("| Role | Model |", text)
        self.assertIn("| lead | claude-sonnet-5-5 |", text)
        self.assertIn("not a bill", text)
        self.assertIn("Actual billed spend:** null", text)

    def test_session_selection_by_id_and_absent_session(self) -> None:
        self._standard_fixture()
        code, rep, _ = self._run("--json", "--session", self.SID[:8])
        self.assertEqual(code, 0)
        self.assertEqual(rep["session"]["sessionId"], self.SID)
        code, _, text = self._run("--json", "--session", "no-such-session")
        self.assertEqual(code, 1)
        self.assertIn("no matching", text)

    def test_subagent_files_are_not_candidate_main_sessions(self) -> None:
        self._standard_fixture()
        found = cost.select_main_session(self.home / ".claude" / "projects",
                                        None, None)
        self.assertEqual(found, self.proj / f"{self.SID}.jsonl")

    def test_report_never_writes_absolute_paths_or_the_encoded_project_dir(self) -> None:
        self._standard_fixture()
        _, rep, _ = self._run("--json")
        text = json.dumps(rep)
        self.assertNotIn(str(self.home), text)
        self.assertNotIn("-work-repo", text)
        self.assertNotIn("/work/repo", text)
        self.assertEqual(rep["session"]["cwd"], "repo")
        self.assertEqual(rep["session"]["cwdSha256"],
                         hashlib.sha256(b"/work/repo").hexdigest()[:8])
        self.assertEqual(rep["session"]["logPath"], f"{self.SID}.jsonl")
        paths = {f["path"] for f in rep["files"]}
        self.assertIn(f"{self.SID}.jsonl", paths)
        self.assertIn(f"{self.SID}/subagents/agent-{self.AID}.jsonl", paths)

    def test_markdown_report_carries_cwd_basename_and_digest_only(self) -> None:
        self._standard_fixture()
        _, _, text = self._run("--markdown")
        self.assertIn("cwd `repo` (cwdSha256 `", text)
        self.assertNotIn("/work/repo", text)
        self.assertNotIn(str(self.home), text)

    def test_read_errors_name_the_errno_not_the_path(self) -> None:
        missing = self.home / "no-such-dir" / "x.jsonl"
        log = cost.read_claude_log(missing, "main")
        self.assertEqual(log.error, "No such file or directory")
        meta = self.home / "agent-x.meta.json"
        meta.write_text("{}", encoding="utf-8")
        with mock.patch.object(cost.Path, "read_text",
                               side_effect=PermissionError(13, "Permission denied", str(meta))):
            agent_type, err = cost._read_meta_agent_type(meta)
        self.assertEqual((agent_type, err), ("", "Permission denied"))
        self.assertNotIn(str(self.home), err)

    def _append_main(self, line: str) -> None:
        p = self.proj / f"{self.SID}.jsonl"
        p.write_text(p.read_text(encoding="utf-8") + line + "\n", encoding="utf-8")

    def _priced_with(self, edit) -> dict:
        prices = {k: (dict(v) if isinstance(v, dict) else v) for k, v in self.PRICES.items()}
        edit(prices["claude-sonnet-5-5"])
        return prices

    def test_catalog_entry_without_output_price_is_unpriced_not_free(self) -> None:
        prices = self._priced_with(lambda e: e.pop("output"))
        self._main_file([self._rec("msg_m1", "req_m1", "claude-sonnet-5-5")])
        with mock.patch.object(cost, "load_prices", return_value=prices):
            _, rep, _ = self._run("--json")
        self.assertFalse(rep["publicRateUsdEquivalent"]["complete"])
        self.assertEqual(rep["publicRateUsdEquivalent"]["usd"], 0.0)
        unknown = {u["model"]: u for u in rep["unknownPrices"]}
        self.assertEqual(unknown["claude-sonnet-5-5"]["reason"], "incomplete-price-entry")
        self.assertFalse(self._rows(rep)[("lead", "claude-sonnet-5-5")]["priced"])

    def test_missing_cache_price_is_priced_with_a_recorded_assumption(self) -> None:
        prices = self._priced_with(lambda e: e.pop("cache_read"))
        self._main_file([self._rec("msg_m1", "req_m1", "claude-sonnet-5-5")])
        with mock.patch.object(cost, "load_prices", return_value=prices):
            _, rep, _ = self._run("--json")
        self.assertTrue(rep["publicRateUsdEquivalent"]["complete"])
        assumed = {(a["model"], a["component"]): a for a in rep["priceAssumptions"]}
        self.assertEqual(assumed[("claude-sonnet-5-5", "cache_read")]["tokens"], 1_000)
        self.assertIn("10% of the input rate", assumed[("claude-sonnet-5-5", "cache_read")]["assumption"])
        # cache reads at 10% of input (2e-6 * 0.1), everything else at the catalog rate.
        expected = 10 * 2e-6 + 100 * 1e-5 + 1_000 * 2e-7 + 50 * 2.5e-6
        self.assertAlmostEqual(rep["publicRateUsdEquivalent"]["usd"], expected)

    def test_zero_catalog_cache_price_is_kept_as_zero(self) -> None:
        prices = self._priced_with(lambda e: e.update(cache_read=0.0))
        self._main_file([self._rec("msg_m1", "req_m1", "claude-sonnet-5-5")])
        with mock.patch.object(cost, "load_prices", return_value=prices):
            _, rep, _ = self._run("--json")
        self.assertEqual(rep["priceAssumptions"], [])
        self.assertAlmostEqual(rep["publicRateUsdEquivalent"]["usd"],
                               10 * 2e-6 + 100 * 1e-5 + 50 * 2.5e-6)

    def test_inferred_cache_write_split_is_listed_as_an_assumption(self) -> None:
        partial = dict(self.USAGE, cache_creation_input_tokens=40,
                       cache_creation={"ephemeral_1h_input_tokens": 8})
        self._main_file([self._rec("msg_p", "req_p", "claude-sonnet-5-5", usage=partial)])
        _, rep, _ = self._run("--json")
        assumed = {(a["model"], a["component"]): a["tokens"] for a in rep["priceAssumptions"]}
        self.assertEqual(assumed[("claude-sonnet-5-5", "cache_write_split")], 40)

    def test_subagent_referenced_by_main_transcript_without_transcript_is_incomplete(self) -> None:
        self._standard_fixture()
        self._append_main(json.dumps({"type": "user", "sessionId": self.SID,
                                      "toolUseResult": {"agentId": "deadbeef99"}}))
        code, rep, _ = self._run("--json")
        self.assertEqual(code, 0)
        self.assertFalse(rep["complete"])
        entries = [i for i in rep["inaccessible"] if i["id"] == "deadbeef99"]
        self.assertEqual([i["kind"] for i in entries], ["subagent"])
        self.assertIn("toolUseResult.agentId", entries[0]["reason"])
        self.assertTrue(any("deadbeef99" in r for r in rep["incompleteReasons"]))
        code, _, _ = self._run("--json", "--strict")
        self.assertEqual(code, 3)

    def test_referenced_subagent_with_a_transcript_keeps_the_report_complete(self) -> None:
        self._standard_fixture()
        self._append_main(json.dumps({"type": "user", "sessionId": self.SID,
                                      "toolUseResult": {"agentId": self.AID}}))
        _, rep, _ = self._run("--json")
        self.assertTrue(rep["complete"])
        self.assertFalse(any(i["id"] == self.AID for i in rep["inaccessible"]))

    def test_referenced_subagent_with_only_metadata_is_listed_once(self) -> None:
        self._standard_fixture()
        (self.sub_dir / "agent-ccccdddd00000001.meta.json").write_text(
            json.dumps({"agentType": "general-purpose"}), encoding="utf-8")
        self._append_main(json.dumps({"type": "user", "sessionId": self.SID,
                                      "toolUseResult": {"agentId": "ccccdddd00000001"}}))
        _, rep, _ = self._run("--json")
        entries = [i for i in rep["inaccessible"] if i["id"] == "ccccdddd00000001"]
        self.assertEqual(len(entries), 1)
        self.assertFalse(rep["complete"])

    def test_negative_boolean_and_string_counts_are_excluded_and_flagged(self) -> None:
        def usage(**over):
            return dict(self.USAGE, **over)
        self._main_file([
            self._rec("msg_ok", "req_ok", "claude-sonnet-5-5"),
            self._rec("msg_neg", "req_neg", "claude-sonnet-5-5", usage=usage(input_tokens=-3)),
            self._rec("msg_bool", "req_bool", "claude-sonnet-5-5", usage=usage(output_tokens=True)),
            self._rec("msg_str", "req_str", "claude-sonnet-5-5", usage=usage(output_tokens="12")),
            json.dumps({"type": "assistant", "requestId": "req_k", "timestamp": "2026-01-01T00:00:01Z",
                        "message": {"id": "msg_k", "model": "claude-sonnet-5-5", "usage": "oops"}}),
        ])
        code, rep, _ = self._run("--json")
        self.assertEqual(code, 0)
        self.assertEqual(rep["malformedUsage"], 4)
        self.assertFalse(rep["complete"])
        self.assertTrue(any("excluded from totals" in r for r in rep["incompleteReasons"]))
        # Only the valid record reaches the totals: no -3, no True-as-1, no "12".
        self.assertEqual(rep["measuredTokens"]["input"], 10)
        self.assertEqual(rep["measuredTokens"]["output"], 100)
        main_entry = [f for f in rep["files"] if f["kind"] == "main"][0]
        self.assertEqual(main_entry["malformedUsageRecords"], 4)
        self.assertEqual(main_entry["usageRecords"], 1)

    def test_missing_cache_write_part_is_derived_from_the_total(self) -> None:
        partial = dict(self.USAGE, cache_creation_input_tokens=40,
                       cache_creation={"ephemeral_1h_input_tokens": 8})
        self._main_file([self._rec("msg_p", "req_p", "claude-sonnet-5-5", usage=partial)])
        _, rep, _ = self._run("--json")
        self.assertEqual(rep["measuredTokens"]["cacheWrite"], 32)
        self.assertEqual(rep["measuredTokens"]["cacheWrite1h"], 8)
        self.assertEqual(rep["malformedUsage"], 0)
        self.assertTrue(rep["complete"])

    def test_cache_write_part_that_cannot_be_placed_is_malformed(self) -> None:
        orphan_5m = {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0,
                     "cache_creation": {"ephemeral_5m_input_tokens": 5}}
        bigger_1h = {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0,
                     "cache_creation_input_tokens": 4,
                     "cache_creation": {"ephemeral_1h_input_tokens": 8}}
        self._main_file([
            self._rec("msg_ok", "req_ok", "claude-sonnet-5-5"),
            self._rec("msg_o", "req_o", "claude-sonnet-5-5", usage=orphan_5m),
            self._rec("msg_b", "req_b", "claude-sonnet-5-5", usage=bigger_1h),
        ])
        _, rep, _ = self._run("--json")
        self.assertEqual(rep["malformedUsage"], 2)
        self.assertFalse(rep["complete"])
        self.assertEqual(rep["measuredTokens"]["cacheWrite"], 50)
        self.assertEqual(rep["measuredTokens"]["cacheWrite1h"], 0)

    def test_legacy_total_only_usage_still_counts_as_five_minute_writes(self) -> None:
        legacy = {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 7}
        self.assertEqual(cost.run_usage_tuple(legacy), ((1, 1, 0, 7, 0), False))
        self._main_file([self._rec("msg_l", "req_l", "claude-sonnet-5-5", usage=legacy)])
        _, rep, _ = self._run("--json")
        self.assertEqual(rep["measuredTokens"]["cacheWrite"], 7)
        self.assertTrue(rep["complete"])

    def _rec_without_request_id(self, msg_id: str, usage=None, uuid: str = "x") -> str:
        d = json.loads(self._rec(msg_id, "unused", "claude-sonnet-5-5", usage=usage, uuid=uuid))
        del d["requestId"]
        return json.dumps(d)

    def test_same_message_with_and_without_request_id_counts_once_main_first(self) -> None:
        self._main_file([self._rec("msg_d", "req_d", "claude-sonnet-5-5", uuid="a")])
        self._sub_file(self.AID, [self._rec_without_request_id("msg_d", uuid="b")])
        _, rep, _ = self._run("--json")
        self.assertEqual(rep["measuredTokens"]["input"], 10)
        self.assertEqual(rep["measuredTokens"]["output"], 100)
        self.assertEqual(rep["dedupe"]["duplicatesIgnored"], 1)

    def test_same_message_with_and_without_request_id_counts_once_id_only_first(self) -> None:
        self._main_file([self._rec_without_request_id("msg_d", uuid="a")])
        self._sub_file(self.AID, [self._rec("msg_d", "req_d", "claude-sonnet-5-5",
                                            sidechain=True, uuid="b")])
        _, rep, _ = self._run("--json")
        self.assertEqual(rep["measuredTokens"]["input"], 10)
        self.assertEqual(rep["measuredTokens"]["output"], 100)
        self.assertEqual(rep["dedupe"]["duplicatesIgnored"], 1)

    def test_id_only_copy_keeps_the_largest_streamed_total(self) -> None:
        early = dict(self.USAGE, output_tokens=8)
        late = dict(self.USAGE, output_tokens=431)
        self._main_file([self._rec("msg_s", "req_s", "claude-sonnet-5-5", usage=early, uuid="a")])
        self._sub_file(self.AID, [self._rec_without_request_id("msg_s", usage=late, uuid="b")])
        _, rep, _ = self._run("--json")
        self.assertEqual(rep["measuredTokens"]["output"], 431)

    def test_distinct_request_ids_on_one_message_stay_distinct(self) -> None:
        # Control: with no id-only copy anywhere, two requestIds are two calls.
        self._main_file([self._rec("msg_t", "req_1", "claude-sonnet-5-5", uuid="a"),
                         self._rec("msg_t", "req_2", "claude-sonnet-5-5", uuid="b")])
        _, rep, _ = self._run("--json")
        self.assertEqual(rep["measuredTokens"]["input"], 20)
        self.assertEqual(rep["dedupe"]["duplicatesIgnored"], 0)


if __name__ == "__main__":
    unittest.main()


MODEL = "test-opus-x"


class CacheReportingTests(unittest.TestCase):
    """A long agent session is almost entirely cache reads, billed at a fraction of the
    input rate. Without that shown, a large total reads as a pricing bug rather than as
    volume -- these cover the numbers that make it checkable."""

    PRICES = {
        "test-opus-x": {
            "input": 5e-6,
            "output": 25e-6,
            "cache_read": 0.5e-6,
            "cache_write": 6.25e-6,
        }
    }

    def _bucket(self) -> cost.Bucket:
        return cost.Bucket(input=1_000, output=2_000,
                           cache_read=1_000_000, cache_write=100_000)

    def test_cost_parts_split_by_billing_component(self) -> None:
        parts = self._bucket().cost_parts(MODEL, self.PRICES)
        self.assertAlmostEqual(parts["input"], 1_000 * 5e-6)
        self.assertAlmostEqual(parts["output"], 2_000 * 25e-6)
        self.assertAlmostEqual(parts["cache_read"], 1_000_000 * 0.5e-6)
        self.assertAlmostEqual(parts["cache_write"], 100_000 * 6.25e-6)

    def test_parts_sum_to_the_reported_cost(self) -> None:
        b = self._bucket()
        parts = b.cost_parts(MODEL, self.PRICES)
        self.assertAlmostEqual(
            parts["input"] + parts["output"] + parts["cache_read"] + parts["cache_write"],
            b.cost(MODEL, self.PRICES),
        )

    def test_hit_rate_counts_every_prompt_token_exactly_once(self) -> None:
        b = self._bucket()
        self.assertEqual(b.prompt_tokens(), 1_000 + 1_000_000 + 100_000)
        self.assertAlmostEqual(b.cache_hit_rate(), 1_000_000 / 1_101_000)
        self.assertNotIn(b.output, (b.prompt_tokens(),), "output is not a prompt token")

    def test_hit_rate_is_none_when_there_was_no_prompt(self) -> None:
        self.assertIsNone(cost.Bucket(output=5).cache_hit_rate())

    def test_savings_is_the_counterfactual_against_full_input_price(self) -> None:
        agg = cost.SessionAgg(harness="h", path="p")
        agg.by_model[MODEL] = self._bucket()
        # 1M cached tokens: $0.50 as cache reads, $5.00 as fresh input.
        self.assertAlmostEqual(agg.cache_savings(self.PRICES), 5.0 - 0.5)

    def test_unpriced_model_contributes_nothing_rather_than_crashing(self) -> None:
        # price_for falls back to longest-substring matching, so an "unknown" name must
        # share no substring with a catalog key to actually be unknown.
        unknown = "qqq"
        agg = cost.SessionAgg(harness="h", path="p")
        agg.by_model[unknown] = self._bucket()
        self.assertIsNone(self._bucket().cost_parts(unknown, self.PRICES))
        self.assertEqual(agg.cache_savings(self.PRICES), 0.0)
        self.assertEqual(agg.cost_parts(self.PRICES)["cache_read"], 0.0)

    def test_savings_sums_across_models_at_their_own_rates(self) -> None:
        prices = dict(self.PRICES)
        prices["test-cheap-x"] = {"input": 1e-6, "output": 2e-6,
                           "cache_read": 0.1e-6, "cache_write": 1.25e-6}
        agg = cost.SessionAgg(harness="h", path="p")
        agg.by_model[MODEL] = cost.Bucket(cache_read=1_000_000)
        agg.by_model["test-cheap-x"] = cost.Bucket(cache_read=1_000_000)
        # $4.50 saved on the dear model, $0.90 on the cheap one.
        self.assertAlmostEqual(agg.cache_savings(prices), 4.5 + 0.9)


class ClaudeCodeUsageTests(unittest.TestCase):
    """Claude Code writes one jsonl line per content block of a response and repeats
    the same usage on each, and a streaming assistant message reports a growing
    output_tokens as the answer is produced. Summing every line bills the same API
    call several times over. A call is identified by (message.id, requestId) and
    the last line for it wins."""

    PRICES = {
        "test-opus-x": {
            "input": 4e-6,
            "output": 2e-5,
            "cache_read": 2e-7,
            "cache_write": 5e-6,
            "cache_write_1h": 8e-6,
        }
    }

    def _line(self, msg_id: str, request_id: str, uuid: str, usage: dict) -> str:
        return json.dumps({
            "type": "assistant",
            "uuid": uuid,
            "requestId": request_id,
            "timestamp": "2026-01-01T00:00:00.000Z",
            "message": {"id": msg_id, "model": MODEL, "usage": usage},
        })

    def _agg(self, lines: list[str]) -> cost.SessionAgg:
        agg = cost.SessionAgg(harness="claude-code", path="p")
        for line in lines:
            cost.parse_claude_code(line, agg)
        return agg

    def test_duplicate_content_block_lines_are_counted_once(self) -> None:
        usage = {"input_tokens": 2, "output_tokens": 547,
                 "cache_read_input_tokens": 28_588,
                 "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                    "ephemeral_1h_input_tokens": 42_771}}
        lines = [self._line("msg_1", "req_1", f"u{i}", usage) for i in range(4)]
        agg = self._agg(lines)
        b = agg.by_model[MODEL]
        self.assertEqual(b.input, 2)
        self.assertEqual(b.output, 547)
        self.assertEqual(b.cache_read, 28_588)
        self.assertEqual(b.cache_write_1h, 42_771)
        self.assertEqual(b.total_tokens(), 2 + 547 + 28_588 + 42_771)

    def test_distinct_calls_are_still_counted_separately(self) -> None:
        usage = {"input_tokens": 2, "output_tokens": 100,
                 "cache_read_input_tokens": 1_000,
                 "cache_creation": {"ephemeral_5m_input_tokens": 500,
                                    "ephemeral_1h_input_tokens": 0}}
        lines = [self._line("msg_1", "req_1", "u1", usage),
                 self._line("msg_2", "req_2", "u2", usage)]
        agg = self._agg(lines)
        b = agg.by_model[MODEL]
        self.assertEqual(b.input, 4)
        self.assertEqual(b.output, 200)
        self.assertEqual(b.cache_read, 2_000)

    def test_last_line_wins_when_output_grows_while_streaming(self) -> None:
        early = {"input_tokens": 2, "output_tokens": 8,
                 "cache_read_input_tokens": 60_040,
                 "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                    "ephemeral_1h_input_tokens": 8_733}}
        late = dict(early, output_tokens=431)
        agg = self._agg([self._line("msg_1", "req_1", "u1", early),
                         self._line("msg_1", "req_1", "u2", late)])
        self.assertEqual(agg.by_model[MODEL].output, 431)

    def test_one_hour_cache_writes_are_priced_at_the_one_hour_rate(self) -> None:
        b = cost.Bucket(cache_write=1_000, cache_write_1h=1_000)
        parts = b.cost_parts(MODEL, self.PRICES)
        self.assertAlmostEqual(parts["cache_write"], 1_000 * 5e-6)
        self.assertAlmostEqual(parts["cache_write_1h"], 1_000 * 8e-6)
        self.assertAlmostEqual(
            parts["input"] + parts["output"] + parts["cache_read"]
            + parts["cache_write"] + parts["cache_write_1h"],
            b.cost(MODEL, self.PRICES),
        )

    def test_one_hour_cache_writes_fall_back_to_twice_input(self) -> None:
        prices = {"test-opus-x": {"input": 4e-6, "output": 2e-5,
                                  "cache_read": 2e-7, "cache_write": 5e-6}}
        b = cost.Bucket(cache_write_1h=1_000)
        parts = b.cost_parts(MODEL, prices)
        self.assertAlmostEqual(parts["cache_write_1h"], 1_000 * 8e-6)

    def test_one_hour_cache_writes_count_toward_the_prompt_token_total(self) -> None:
        b = cost.Bucket(input=10, cache_read=100, cache_write=5, cache_write_1h=7)
        self.assertEqual(b.prompt_tokens(), 10 + 100 + 5 + 7)
        self.assertEqual(b.total_tokens(), 10 + 100 + 5 + 7)
