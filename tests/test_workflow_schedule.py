import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkflowScheduleTest(unittest.TestCase):
    def test_daily_forecast_runs_at_1215_beijing_time(self):
        workflow = (
            ROOT / ".github" / "workflows" / "daily-forecast.yml"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "# 12:15 Beijing time, after the daily Sporttery data update.",
            workflow,
        )
        self.assertIn('- cron: "15 4 * * *"', workflow)
        self.assertNotIn('- cron: "30 3 * * *"', workflow)


if __name__ == "__main__":
    unittest.main()
