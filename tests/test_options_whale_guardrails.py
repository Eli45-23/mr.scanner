import re
import unittest
from pathlib import Path

import elite_momentum_scanner as scanner_app


ROOT = Path(__file__).resolve().parents[1]
DEPRECATED_ORDER_FILES = {
    "paper_trade_cli.py",
    "spy_paper_autotrader.py",
    "ai_paper_trade_lab.py",
}


class OptionsWhaleGuardrailTests(unittest.TestCase):
    def test_legacy_momentum_disabled_by_default(self):
        config = scanner_app.load_config(None)
        self.assertTrue(config["enable_options_whale_scanner"])
        self.assertFalse(config["enable_legacy_momentum_scanner"])
        self.assertFalse(any(config["alert_rules"].values()))

    def test_no_new_order_execution_calls_in_options_whale_subsystem(self):
        forbidden = re.compile(r"submit_order|create_order|cancel_order|close_position\(|replace_order|preview-order|submit-paper-order|webull", re.IGNORECASE)
        offenders = []
        paths = list((ROOT / "scanner").glob("options_*.py")) + list((ROOT / "tools").glob("*options*.py"))
        for path in paths:
            rel = str(path.relative_to(ROOT))
            if rel in DEPRECATED_ORDER_FILES or rel.startswith("tests/"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if forbidden.search(text):
                offenders.append(rel)
        self.assertEqual(offenders, [])

    def test_no_forbidden_alert_warnings_in_options_modules(self):
        forbidden = re.compile(r"\b(?:buy this|sell this|enter now|guaranteed|confirmed smart money)\b", re.IGNORECASE)
        offenders = []
        for path in (ROOT / "scanner").glob("options_*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if forbidden.search(text):
                offenders.append(path.name)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
