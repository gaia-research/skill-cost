"""Offline regression tests for validation and transactional price refresh."""

import json
import os
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

import cost


class PriceRefreshTests(unittest.TestCase):
    CATALOG = {
        "test-model": {
            "input_cost_per_token": 0.000001,
            "output_cost_per_token": 0.000002,
            "cache_read_input_token_cost": 0,
            "litellm_provider": "test",
        }
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "prices.json"
        self.old = b'{"old-model": {"input": 1, "output": 2}}\n'
        self.path.write_bytes(self.old)
        patcher = mock.patch.object(cost, "PRICES_JSON", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def response(self, payload):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = payload.encode("utf-8")
        return mock.patch.object(cost.urllib.request, "urlopen", return_value=response)

    def assert_cache_preserved(self):
        self.assertEqual(self.path.read_bytes(), self.old)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_success_replaces_cache_with_pruned_catalog_and_provenance(self):
        self.path.chmod(0o640)
        replace = os.replace

        def checked_replace(source, destination):
            self.assertEqual(Path(source).parent, self.path.parent)
            self.assertEqual(self.path.read_bytes(), self.old)
            self.assertEqual(json.loads(Path(source).read_text())["test-model"]["input"], 1e-6)
            replace(source, destination)

        with self.response(json.dumps(self.CATALOG)), \
             mock.patch.object(cost.os, "replace", side_effect=checked_replace) as swapped:
            prices = cost.refresh_prices(quiet=True)
        swapped.assert_called_once()
        self.assertEqual(json.loads(self.path.read_text()), prices)
        self.assertEqual(prices["test-model"]["cache_read"], 0)
        self.assertEqual(prices["_meta"]["model_count"], 1)
        self.assertEqual(prices["_meta"]["source_url"], cost.LITELLM_URL)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_empty_malformed_and_non_object_catalogs_preserve_cache(self):
        for payload in ["{}", "[]", "null", "{broken", '{"model": {}}']:
            with self.subTest(payload=payload), self.response(payload):
                with self.assertRaises(ValueError):
                    cost.refresh_prices(quiet=True)
                self.assert_cache_preserved()

    def test_invalid_rates_preserve_cache(self):
        for field in ("input_cost_per_token", "output_cost_per_token",
                      "cache_read_input_token_cost", "cache_creation_input_token_cost",
                      "cache_creation_input_token_cost_above_1hr"):
            for value in (-1, True, "1.0", float("nan"), float("inf")):
                entry = {**self.CATALOG["test-model"], field: value}
                with self.subTest(field=field, value=value), \
                     self.response(json.dumps({"test-model": entry})):
                    with self.assertRaises(ValueError):
                        cost.refresh_prices(quiet=True)
                    self.assert_cache_preserved()

    def test_write_or_replace_failure_preserves_cache_and_removes_staging_file(self):
        for target in ("json.dump", "os.fsync", "os.replace"):
            def fail(*args, **kwargs):
                if target == "json.dump":
                    args[1].write('{"partial":')
                raise OSError("simulated failure")

            with self.subTest(target=target), self.response(json.dumps(self.CATALOG)), \
                 mock.patch("cost." + target, side_effect=fail):
                with self.assertRaises(OSError):
                    cost.refresh_prices(quiet=True)
                self.assert_cache_preserved()

    def test_failed_auto_refresh_falls_back_to_unchanged_cache(self):
        with self.response("{}"), \
             mock.patch.object(cost, "_prices_age_days", return_value=100), \
             mock.patch.dict(os.environ, {"SKILL_COST_NO_AUTO_REFRESH": "0"}), \
             mock.patch("sys.stderr", new_callable=StringIO) as err:
            prices = cost.load_prices()
        self.assertEqual(prices, json.loads(self.old))
        self.assertIn("using cached copy", err.getvalue())
        self.assert_cache_preserved()

    def test_write_false_does_not_create_or_modify_files(self):
        with self.response(json.dumps(self.CATALOG)):
            prices = cost.refresh_prices(write=False, quiet=True)
        self.assertIn("test-model", prices)
        self.assert_cache_preserved()

    def test_success_can_create_a_missing_cache(self):
        self.path.unlink()
        with self.response(json.dumps(self.CATALOG)):
            prices = cost.refresh_prices(quiet=True)
        self.assertEqual(json.loads(self.path.read_text()), prices)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_network_failure_preserves_cache(self):
        with mock.patch.object(cost.urllib.request, "urlopen", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                cost.refresh_prices(quiet=True)
        self.assert_cache_preserved()


if __name__ == "__main__":
    unittest.main()
