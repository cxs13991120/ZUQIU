import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkflowScheduleTest(unittest.TestCase):
    WORKFLOWS = ROOT / ".github" / "workflows"

    def read_workflow(self, name):
        return (self.WORKFLOWS / name).read_text(encoding="utf-8")

    def test_beijing_schedule_crons_include_settlement_retry_and_snapshots(self):
        schedules = {
            "daily-forecast.yml": 'cron: "15 4 * * *"',
            "draw-alert-refresh.yml": 'cron: "30 5 * * *"',
            "noon-settlement.yml": 'cron: "45 5 * * *"',
            "email-report.yml": 'cron: "0 6 * * *"',
            "odds-snapshot.yml": 'cron: "*/30 * * * *"',
        }
        for name, cron in schedules.items():
            self.assertIn(cron, self.read_workflow(name))
        self.assertIn('cron: "5 6 * * *"', self.read_workflow("noon-settlement.yml"))

    def test_all_related_workflows_share_the_repository_queue(self):
        contract = "concurrency:\n  group: sporttery-repository\n  cancel-in-progress: false\n  queue: max"
        for name in (
            "daily-forecast.yml",
            "draw-alert-refresh.yml",
            "noon-settlement.yml",
            "email-report.yml",
            "odds-snapshot.yml",
        ):
            self.assertIn(contract, self.read_workflow(name))

    def test_base_forecast_uses_beijing_target_date_and_required_command_order(self):
        text = self.read_workflow("daily-forecast.yml")
        self.assertIn("TZ: Asia/Shanghai", text)
        expected = [
            'TARGET_DATE="$(date +%F)"',
            'python import_sporttery.py --date "$TARGET_DATE"',
            "python build_historical_features.py",
            'python predict_today.py --date "$TARGET_DATE"',
            'python generate_betting_plan.py --date "$TARGET_DATE"',
            'python collect_market_heat.py --date "$TARGET_DATE"',
            'python generate_draw_alert.py --date "$TARGET_DATE"',
            "python draw_alert_ledger.py --settle",
            "python build_site.py",
            "python build_daily_image.py",
        ]
        positions = [text.index(command) for command in expected]
        self.assertEqual(positions, sorted(positions))

    def test_refresh_isolates_all_inputs_and_always_rebuilds_the_report(self):
        text = self.read_workflow("draw-alert-refresh.yml")
        self.assertGreaterEqual(text.count("continue-on-error: true"), 4)
        for command in (
            'TARGET_DATE="$(date +%F)"',
            'python import_sporttery.py --date "$TARGET_DATE"',
            'python predict_today.py --date "$TARGET_DATE"',
            'python collect_market_heat.py --date "$TARGET_DATE"',
            'python generate_draw_alert.py --date "$TARGET_DATE"',
            "python draw_alert_ledger.py --settle",
            "python build_site.py",
            "python build_daily_image.py",
        ):
            self.assertIn(command, text)
        self.assertLess(text.index("python draw_alert_ledger.py --settle"), text.index("python build_site.py"))
        self.assertLess(text.index("python build_site.py"), text.index("python build_daily_image.py"))

    def test_settlement_uses_yesterday_for_results_and_today_for_training(self):
        text = self.read_workflow("noon-settlement.yml")
        expected = [
            'TODAY="$(date +%F)"',
            'SETTLEMENT_DATE="$(date -d \'yesterday\' +%F)"',
            'python update_sporttery_results.py --date "$SETTLEMENT_DATE"',
            "python build_historical_features.py",
            "python generate_betting_plan.py --settle-only",
            "python draw_alert_ledger.py --settle",
            'python draw_model_learning.py --train --date "$TODAY"',
            "python build_site.py",
            "python build_daily_image.py",
        ]
        positions = [text.index(command) for command in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn('python update_sporttery_results.py --date "$TODAY"', text)

    def test_base_refresh_and_settlement_install_learning_and_image_dependencies(self):
        for name in ("daily-forecast.yml", "draw-alert-refresh.yml", "noon-settlement.yml"):
            text = self.read_workflow(name)
            self.assertIn("python -m pip install --quiet -r requirements.txt", text)
            self.assertIn("python -m pip install --quiet pillow", text)
            self.assertIn("fonts-noto-cjk", text)

    def test_commits_include_immutable_learning_and_report_outputs(self):
        required = (
            "data/market_heat_*.json",
            "data/draw_feature_snapshots/*.json",
            "data/models/*.joblib",
            "output/draw_alert*.csv",
            "output/draw_alert*.json",
            "output/draw_model_registry.json",
            "web/index.html",
            "web/daily-report.png",
        )
        for name in ("daily-forecast.yml", "draw-alert-refresh.yml", "noon-settlement.yml"):
            text = self.read_workflow(name)
            for pattern in required:
                self.assertIn(pattern, text)

    def test_email_uses_the_queue_and_does_not_install_learning_or_image_packages(self):
        text = self.read_workflow("email-report.yml")
        self.assertIn("TZ: Asia/Shanghai", text)
        self.assertIn("python send_daily_email.py", text)
        self.assertNotIn("requirements.txt", text)
        self.assertNotIn("pillow", text.lower())


if __name__ == "__main__":
    unittest.main()
