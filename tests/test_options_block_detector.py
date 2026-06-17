import unittest

from scanner.options_block_detector import detect_block_print


class OptionsBlockDetectorTests(unittest.TestCase):
    def test_detects_block_print(self):
        result = detect_block_print({"last": 4.0}, [{"size": 300, "price": 4.0}], {"min_premium": 100000})
        self.assertTrue(result["is_possible_block"])
        self.assertIn("Block prints may be spreads", result["block_warning"])


if __name__ == "__main__":
    unittest.main()
