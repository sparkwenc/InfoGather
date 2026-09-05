import shutil
import subprocess
import unittest
from pathlib import Path

from infogather.paths import WEB_DIR


@unittest.skipUnless(shutil.which("node"), "node is required for JavaScript checks")
class WebModuleTests(unittest.TestCase):
    def test_application_request_races(self) -> None:
        subprocess.run(
            ["node", "--test", Path(__file__).with_name("test_app.mjs")],
            check=True,
        )

    def test_modules_parse_and_api_forwards_abort_signal(self) -> None:
        modules = [WEB_DIR / "js" / name for name in ("api.js", "app.js", "ui.js")]
        for module in modules:
            subprocess.run(["node", "--check", module], check=True)

        script = """
            import { readFile } from "node:fs/promises";
            const source = await readFile(process.argv[1], "utf8");
            const url = `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
            const api = await import(url);
            const calls = [];
            globalThis.fetch = async (requestUrl, options) => {
              calls.push([requestUrl, options]);
              return { ok: true, status: 200, json: async () => ({}) };
            };
            const controller = new AbortController();
            await api.getInsStatus(controller.signal);
            await api.getEntries(new URLSearchParams("q=test"), controller.signal);
            await api.getTagTree(new URLSearchParams(), controller.signal);
            if (calls.some(([, options]) => options.signal !== controller.signal)) {
              process.exit(1);
            }
            globalThis.fetch = async () => ({
              ok: true,
              status: 200,
              json: async () => { throw new SyntaxError("truncated JSON"); }
            });
            try {
              await api.getEntries(new URLSearchParams());
              process.exit(1);
            } catch (error) {
              if (!(error instanceof SyntaxError)) process.exit(1);
            }
            for (const json of [
              async () => null,
              async () => { throw new SyntaxError("HTML error page"); }
            ]) {
              globalThis.fetch = async () => ({ ok: false, status: 409, json });
              try {
                await api.restoreEntry("expired-token");
                process.exit(1);
              } catch (error) {
                if (error.status !== 409) process.exit(1);
              }
            }
        """
        subprocess.run(
            ["node", "--input-type=module", "-e", script, modules[0]],
            check=True,
        )
