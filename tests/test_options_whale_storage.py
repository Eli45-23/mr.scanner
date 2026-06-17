import tempfile
import unittest
from pathlib import Path

from scanner.options_whale_storage import OptionsWhaleStorage


class OptionsWhaleStorageTests(unittest.TestCase):
    def test_jsonl_and_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = OptionsWhaleStorage(Path(tmp))
            storage.append_alert({"symbol": "AAPL", "score": 90})
            self.assertEqual(storage.latest_alerts()[0]["symbol"], "AAPL")
            self.assertTrue(storage.export_json(Path(tmp) / "out.json", storage.latest_alerts()).exists())
            self.assertTrue(storage.export_csv(Path(tmp) / "out.csv", storage.latest_alerts()).exists())


if __name__ == "__main__":
    unittest.main()
