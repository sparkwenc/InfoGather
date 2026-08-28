import os
import subprocess
import sys
import unittest
from argparse import Namespace
from unittest import mock

from infogather.cli import _cmd_ins


class CliModuleEntryPointTests(unittest.TestCase):
    def test_ins_delegates_to_shared_ingestion(self) -> None:
        with mock.patch("infogather.cli.run_ingestion") as ingestion:
            result = _cmd_ins(Namespace(db_path="entries.db", conf="config.toml"))

        self.assertEqual(result, 0)
        ingestion.assert_called_once_with("entries.db", "config.toml")

    def test_module_help_executes_main(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = env.get("PYTHONPATH", "src")
        result = subprocess.run(
            [sys.executable, "-m", "infogather.cli", "--help"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage:", result.stdout.lower())
        self.assertIn("ins", result.stdout)
