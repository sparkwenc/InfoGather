import os
import subprocess
import sys
import unittest


class CliModuleEntryPointTests(unittest.TestCase):
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

