"""Regressao: configuracao do EasyPanel sem arquivo .env."""

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "worki_env", Path(__file__).resolve().parents[1] / "integracoes/supabase/env.py"
)
env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(env)


class EnvironmentTests(unittest.TestCase):
    def load(self, text=None, variables=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            if text is not None:
                path.write_text(text, encoding="utf-8")
            with patch.object(env, "ARQUIVO", path), patch.dict(
                os.environ, variables or {}, clear=True
            ):
                return env.carregar()

    def test_easypanel_without_dotenv(self):
        values = {"SUPABASE_URL": "https://test.invalid", "EVOLUTION_INSTANCE": "test"}
        self.assertEqual(self.load(variables=values), values)

    def test_new_environment_key_is_included(self):
        self.assertEqual(self.load("A=file\n", {"B": "environment"}),
                         {"A": "file", "B": "environment"})

    def test_environment_overrides_file(self):
        self.assertEqual(self.load("A=old\n", {"A": "new"})["A"], "new")

    def test_empty_environment_does_not_restore_old_secret(self):
        self.assertEqual(self.load("TOKEN=old\n", {"TOKEN": ""})["TOKEN"], "")

    def test_local_file_remains_supported(self):
        self.assertEqual(self.load('# comment\nA="value"\nB=one=two\n'),
                         {"A": "value", "B": "one=two"})

    def test_missing_and_masked_values_fail_closed(self):
        for value in ("", "***", " *** "):
            with self.subTest(value=value), patch.object(env, "CFG", {"TOKEN": value}):
                with self.assertRaises(SystemExit) as error:
                    env.chave("TOKEN")
                self.assertIn("TOKEN", str(error.exception))

    def test_valid_key_is_returned_without_logging_value(self):
        with patch.object(env, "CFG", {"TOKEN": "test-value"}):
            self.assertEqual(env.chave("TOKEN"), "test-value")


if __name__ == "__main__":
    unittest.main()
