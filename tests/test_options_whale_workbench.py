import json
import unittest

from tools.options_whale_workbench import HTML_TEMPLATE, WorkbenchHandler


class OptionsWhaleWorkbenchTests(unittest.TestCase):
    def test_html_template_includes_dashboard_links(self):
        html = HTML_TEMPLATE.format(
            main_url="http://127.0.0.1:8765",
            outcome_url="http://127.0.0.1:8775",
        )
        self.assertIn("Options Whale Workbench", html)
        self.assertIn("http://127.0.0.1:8765", html)
        self.assertIn("http://127.0.0.1:8775", html)

    def test_handler_default_links_are_json_serializable(self):
        payload = {
            "main_dashboard": WorkbenchHandler.main_url,
            "outcome_dashboard": WorkbenchHandler.outcome_url,
        }
        encoded = json.dumps(payload)
        self.assertIn("main_dashboard", encoded)
        self.assertIn("outcome_dashboard", encoded)


if __name__ == "__main__":
    unittest.main()
