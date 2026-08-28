import shutil
import subprocess
import unittest

from infogather.paths import WEB_DIR


@unittest.skipUnless(shutil.which("node"), "node is required for JavaScript checks")
class WebModuleTests(unittest.TestCase):
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
        """
        subprocess.run(
            ["node", "--input-type=module", "-e", script, modules[0]],
            check=True,
        )
