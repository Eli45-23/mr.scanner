import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tools import compact_options_outcomes as compact_tool


class CompactOptionsOutcomesTests(unittest.TestCase):
    def row(self, key, status="pending", move=0.2):
        return {
            "alert_key": key,
            "outcome_status": status,
            "completed_window_count": 1 if status in {"ok", "partial"} else 0,
            "windows": [{"status": "ok", "move_pct": move, "favorable": True}] if status in {"ok", "partial"} else [],
        }

    def dirty_completed(self, key):
        row = self.row(key, status="ok")
        row["completed_window_count"] = None
        row["windows"] = [{"status": "missing_bar", "move_pct": None, "favorable": None}]
        row["max_favorable_move_pct"] = None
        return row

    def write_rows(self, path, rows):
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    def test_compact_keeps_latest_by_alert_key_and_removes_dirty_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outcomes.jsonl"
            self.write_rows(path, [
                self.row("dup", status="pending"),
                self.row("dup", status="ok", move=0.4),
                self.dirty_completed("dirty"),
                self.row("pending", status="pending"),
            ])
            result = compact_tool.compact_outcomes(path)
        self.assertEqual(result["raw_count"], 4)
        self.assertEqual(result["unique_count"], 3)
        self.assertEqual(result["clean_count"], 2)
        self.assertEqual(result["duplicate_count"], 1)
        self.assertEqual(result["dirty_completed_removed"], 1)
        keys = [row["alert_key"] for row in result["rows"]]
        self.assertEqual(keys, ["dup", "pending"])
        self.assertEqual(result["rows"][0]["outcome_status"], "ok")

    def test_cli_defaults_to_separate_output_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "outcomes.jsonl"
            output = Path(tmp) / "clean.jsonl"
            self.write_rows(source, [self.row("a"), self.row("a", status="ok")])
            with mock.patch("sys.argv", ["compact_options_outcomes.py", "--path", str(source), "--output", str(output)]):
                with redirect_stdout(io.StringIO()):
                    rc = compact_tool.main()
            source_rows = source.read_text(encoding="utf-8").splitlines()
            output_rows = output.read_text(encoding="utf-8").splitlines()
        self.assertEqual(rc, 0)
        self.assertEqual(len(source_rows), 2)
        self.assertEqual(len(output_rows), 1)


if __name__ == "__main__":
    unittest.main()
