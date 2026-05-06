from __future__ import annotations

import unittest

from codex_usage.app_server import _extract_weekly_limit


class AppServerParsingTests(unittest.TestCase):
    def test_extracts_rounded_weekly_limit_fallback(self) -> None:
        weekly = _extract_weekly_limit(
            {
                "rateLimitsByLimitId": {
                    "codex": {
                        "secondary": {
                            "usedPercent": 24,
                            "windowDurationMins": 10080,
                            "resetsAt": 1777609978,
                        }
                    }
                }
            }
        )

        self.assertIsNotNone(weekly)
        self.assertEqual(weekly.used_percent, 24.0)
        self.assertEqual(weekly.window_minutes, 10080)
        self.assertEqual(weekly.source, "app_server")


if __name__ == "__main__":
    unittest.main()
