import csv
from datetime import date, datetime, timedelta
from pathlib import Path

from import_sporttery import fetch_matches


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"


def parse_score(value: str) -> tuple[str, str] | None:
    value = (value or "").strip()
    if ":" not in value:
        return None
    left, right = value.split(":", 1)
    if not left.strip().isdigit() or not right.strip().isdigit():
        return None
    return left.strip(), right.strip()


def read_existing(path: Path) -> dict[tuple[str, str, str], dict]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return {(row["date"], row["team_a"], row["team_b"]): row for row in csv.DictReader(fh)}


def update_results(target_date: date) -> Path:
    path = DATA_DIR / "bet_results.csv"
    rows = read_existing(path)
    matches = fetch_matches(target_date)
    for item in matches:
        if str(item.get("matchResultStatus", "")) != "2":
            continue
        full = parse_score(item.get("sectionsNo999", ""))
        half = parse_score(item.get("sectionsNo1", ""))
        if full is None:
            continue
        if half is None:
            half = ("0", "0")
        key = (target_date.isoformat(), item.get("homeTeam", ""), item.get("awayTeam", ""))
        rows[key] = {
            "date": key[0],
            "team_a": key[1],
            "team_b": key[2],
            "home_goals": full[0],
            "away_goals": full[1],
            "half_home_goals": half[0],
            "half_away_goals": half[1],
        }

    fields = ["date", "team_a", "team_b", "home_goals", "away_goals", "half_home_goals", "half_away_goals"]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for key in sorted(rows):
            writer.writerow(rows[key])
    return path


def main() -> int:
    import argparse

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    parser = argparse.ArgumentParser(description="从竞彩网抓取已开奖赛果并更新结算数据。")
    parser.add_argument("--date", default=yesterday)
    args = parser.parse_args()

    target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    path = update_results(target_date)
    print(f"Updated results: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
