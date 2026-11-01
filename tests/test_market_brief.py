import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import market_brief as mb  # noqa: E402

ROOT = Path(mb.__file__).resolve().parent


class TempYaml:
    def __init__(self, text: str):
        self.text = text

    def __enter__(self) -> Path:
        self.dir = tempfile.TemporaryDirectory()
        path = Path(self.dir.name) / "file.yaml"
        path.write_text(self.text)
        return path

    def __exit__(self, *exc):
        self.dir.cleanup()


class RepoFilesTest(unittest.TestCase):
    def test_repo_files_are_valid(self):
        self.assertTrue(mb.load_holdings(ROOT / "stocks.yaml"))
        mb.load_config(ROOT / "config.yaml")


class HoldingsTest(unittest.TestCase):
    def load(self, text):
        with TempYaml(text) as path:
            return mb.load_holdings(path)

    def test_normalizes_symbol(self):
        self.assertEqual(self.load('holdings:\n  - {symbol: " brk.b ", name: Berkshire}')[0]["symbol"], "BRK.B")

    def test_unquoted_boolean_symbol_rejected(self):
        with self.assertRaisesRegex(mb.ConfigError, "quoted"):
            self.load("holdings:\n  - {symbol: ON, name: onsemi}")

    def test_duplicate_rejected(self):
        with self.assertRaisesRegex(mb.ConfigError, "more than once"):
            self.load('holdings:\n  - {symbol: "AAPL", name: Apple}\n  - {symbol: "aapl", name: Apple}')

    def test_missing_name_rejected(self):
        with self.assertRaisesRegex(mb.ConfigError, "needs a 'name'"):
            self.load('holdings:\n  - {symbol: "AAPL"}')

    def test_empty_list_rejected(self):
        with self.assertRaises(mb.ConfigError):
            self.load("holdings: []")


class ConfigTest(unittest.TestCase):
    BASE = "alerts: {price_move_pct: 3, volume_spike_ratio: 2, week52_proximity_pct: 2, news_topics: [earnings]}\n"

    def load(self, extra=""):
        with TempYaml(self.BASE + extra) as path:
            return mb.load_config(path)

    def test_defaults(self):
        cfg = self.load()
        self.assertEqual(cfg["model"], "claude-opus-5-5")
        self.assertEqual(cfg["effort"], "medium")

    def test_bad_effort(self):
        with self.assertRaises(mb.ConfigError):
            self.load("effort: extreme\n")

    def test_bad_timezone(self):
        with self.assertRaises(mb.ConfigError):
            self.load("timezone: Mars/Olympus\n")

    def test_negative_threshold(self):
        with TempYaml(self.BASE.replace("price_move_pct: 3", "price_move_pct: -1")) as path:
            with self.assertRaises(mb.ConfigError):
                mb.load_config(path)


class OutputTest(unittest.TestCase):
    def test_extract_drops_preamble_and_dedupes_sources(self):
        cite = SimpleNamespace(url="https://example.com/a", title="A")
        content = [
            SimpleNamespace(type="thinking"),
            SimpleNamespace(type="text", text="Let me search.\n", citations=None),
            SimpleNamespace(type="server_tool_use"),
            SimpleNamespace(type="text", text="# Market Brief — Nov 2\n\nApple rose ", citations=None),
            SimpleNamespace(type="text", text="2%.", citations=[cite]),
            SimpleNamespace(type="text", text=" Again.", citations=[cite]),
        ]
        body, sources = mb.extract_brief(content)
        self.assertTrue(body.startswith("# Market Brief"))
        self.assertIn("Apple rose 2%. Again.", body)
        self.assertEqual(sources, [("A", "https://example.com/a")])

    def test_render_includes_sources_and_disclaimer(self):
        when = dt.datetime(2026, 11, 2, 8, 30, tzinfo=dt.timezone.utc)
        md = mb.render_markdown("# Market Brief", [("T [x]", "https://e.com")], "claude-opus-5-5", when)
        self.assertIn("1. [T (x)](https://e.com)", md)
        self.assertIn("not investment advice", md)

    def test_prompt_lists_holdings_and_thresholds(self):
        cfg = mb.load_config(ROOT / "config.yaml")
        prompt = mb.build_prompt(mb.load_holdings(ROOT / "stocks.yaml"), cfg, dt.date(2026, 11, 2))
        self.assertIn("NVDA (NVIDIA)", prompt)
        self.assertIn("3% or more", prompt)
        self.assertIn("Monday, November 2, 2026", prompt)


if __name__ == "__main__":
    unittest.main()
