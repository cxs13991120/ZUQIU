import csv
import json
import math
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
API_URL = "https://webapi.sporttery.cn/gateway/uniform/football/getUniformMatchResultV1.qry"
MATCH_LIST_URL = "https://webapi.sporttery.cn/gateway/uniform/football/getMatchListV1.qry"
ODDS_URL = "https://webapi.sporttery.cn/gateway/uniform/football/getFixedBonusV1.qry"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Referer": "https://www.sporttery.cn/jc/zqsgkj/",
    "Origin": "https://www.sporttery.cn",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}


def fetch_matches(target_date: date) -> list[dict]:
    params = {
        "matchBeginDate": target_date.isoformat(),
        "matchEndDate": target_date.isoformat(),
        "leagueId": "",
        "pageSize": "100",
        "pageNo": "1",
        "isFix": "0",
        "matchPage": "1",
        "pcOrWap": "1",
    }
    url = API_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if str(payload.get("errorCode")) != "0":
        raise RuntimeError(payload.get("errorMessage", "竞彩网接口返回异常"))
    return payload.get("value", {}).get("matchResult", [])


def fetch_selling_matches(target_date: date) -> list[dict]:
    params = {"clientCode": "3001"}
    url = MATCH_LIST_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if str(payload.get("errorCode")) != "0":
        raise RuntimeError(payload.get("errorMessage", "竞彩网在售接口返回异常"))

    selected = []
    for day in payload.get("value", {}).get("matchInfoList", []):
        if day.get("businessDate") != target_date.isoformat():
            continue
        for item in day.get("subMatchList", []):
            if item.get("matchStatus") in {"Selling", "Define"}:
                selected.append(item)
    return selected


def latest_odds_record(records: list[dict]) -> dict:
    if not records:
        return {}
    return records[0]


def fetch_odds(match_id: str) -> dict:
    params = {"matchId": match_id, "clientCode": "3001"}
    url = ODDS_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if str(payload.get("errorCode")) != "0":
        return {}
    history = payload.get("value", {}).get("oddsHistory", {})
    return {
        "had": latest_odds_record(history.get("hadList", [])),
        "hhad": latest_odds_record(history.get("hhadList", [])),
        "ttg": latest_odds_record(history.get("ttgList", [])),
        "hafu": latest_odds_record(history.get("hafuList", [])),
        "crs": latest_odds_record(history.get("crsList", [])),
    }


def active_matches(matches: list[dict]) -> list[dict]:
    rows = []
    for item in matches:
        status = str(item.get("matchResultStatus", ""))
        if status == "2":
            continue
        rows.append(item)
    return rows


def implied_home_edge(home_odds: str, away_odds: str) -> float:
    try:
        home = 1 / float(home_odds)
        away = 1 / float(away_odds)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0
    total = home + away
    if total <= 0:
        return 0.0
    return max(-0.18, min(0.18, (home - away) / total * 0.28))


def team_home(item: dict) -> str:
    return item.get("homeTeam") or item.get("homeTeamAbbName") or item.get("homeTeamAllName") or ""


def team_away(item: dict) -> str:
    return item.get("awayTeam") or item.get("awayTeamAbbName") or item.get("awayTeamAllName") or ""


def league_name(item: dict) -> str:
    return item.get("leagueNameAbbr") or item.get("leagueAbbName") or item.get("leagueName") or item.get("leagueAllName") or ""


def match_number(item: dict) -> str:
    return item.get("matchNumStr") or item.get("matchNum") or ""


def attach_had_odds(matches: list[dict], odds_by_id: dict[str, dict]) -> list[dict]:
    enriched = []
    for item in matches:
        row = dict(item)
        had = odds_by_id.get(str(item.get("matchId", "")), {}).get("had", {})
        row["h"] = row.get("h") or had.get("h", "")
        row["d"] = row.get("d") or had.get("d", "")
        row["a"] = row.get("a") or had.get("a", "")
        enriched.append(row)
    return enriched


def write_fixtures(matches: list[dict], target_date: date) -> Path:
    path = DATA_DIR / "fixtures.csv"
    fields = [
        "date",
        "kickoff_local",
        "stage",
        "team_a",
        "team_b",
        "neutral",
        "venue",
        "odds_a",
        "odds_draw",
        "odds_b",
        "match_num",
        "match_id",
        "pool_status",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for item in matches:
            writer.writerow(
                {
                    "date": target_date.isoformat(),
                    "kickoff_local": match_number(item),
                    "stage": league_name(item),
                    "team_a": team_home(item),
                    "team_b": team_away(item),
                    "neutral": "false",
                    "venue": "竞彩网",
                    "odds_a": item.get("h", ""),
                    "odds_draw": item.get("d", ""),
                    "odds_b": item.get("a", ""),
                    "match_num": match_number(item),
                    "match_id": item.get("matchId", ""),
                    "pool_status": item.get("poolStatus", item.get("matchStatus", "")),
                }
            )
    return path


def load_ratings() -> dict[str, dict]:
    path = DATA_DIR / "team_ratings.csv"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return {row["team"]: row for row in csv.DictReader(fh)}


def write_ratings(matches: list[dict]) -> Path:
    path = DATA_DIR / "team_ratings.csv"
    ratings = load_ratings()
    for item in matches:
        edge = implied_home_edge(item.get("h", ""), item.get("a", ""))
        home = team_home(item)
        away = team_away(item)
        if home and home not in ratings:
            ratings[home] = {
                "team": home,
                "elo": str(round(1850 + edge * 650)),
                "attack": f"{edge:.3f}",
                "defense": "0.000",
                "form": "0.000",
                "injury": "0.000",
                "rest_days": "4",
                "home_adv": "0.080",
            }
        if away and away not in ratings:
            ratings[away] = {
                "team": away,
                "elo": str(round(1850 - edge * 650)),
                "attack": f"{-edge:.3f}",
                "defense": "0.000",
                "form": "0.000",
                "injury": "0.000",
                "rest_days": "4",
                "home_adv": "0.000",
            }
    fields = ["team", "elo", "attack", "defense", "form", "injury", "rest_days", "home_adv"]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for key in sorted(ratings):
            writer.writerow(ratings[key])
    return path


def write_odds(matches: list[dict], target_date: date) -> Path:
    path = DATA_DIR / f"sporttery_odds_{target_date.isoformat()}.json"
    odds = {}
    for item in matches:
        match_id = str(item.get("matchId", ""))
        if match_id:
            odds[match_id] = fetch_odds(match_id)
    path.write_text(json.dumps(odds, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def collect_odds(matches: list[dict]) -> dict[str, dict]:
    odds = {}
    for item in matches:
        match_id = str(item.get("matchId", ""))
        if match_id:
            odds[match_id] = fetch_odds(match_id)
    return odds


def write_odds_data(odds: dict[str, dict], target_date: date) -> Path:
    path = DATA_DIR / f"sporttery_odds_{target_date.isoformat()}.json"
    path.write_text(json.dumps(odds, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="从竞彩网官方接口导入当天竞彩足球比赛。")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--include-finished", action="store_true", help="包含已开奖比赛，默认排除。")
    args = parser.parse_args()

    target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    selected = fetch_selling_matches(target_date)
    matches = selected
    if args.include_finished:
        matches = fetch_matches(target_date)
        selected = matches
    odds_data = collect_odds(selected)
    selected = attach_had_odds(selected, odds_data)
    fixtures_path = write_fixtures(selected, target_date)
    ratings_path = write_ratings(selected)
    odds_path = write_odds_data(odds_data, target_date)
    print(f"竞彩网返回比赛: {len(matches)}")
    print(f"导入未开奖比赛: {len(selected)}")
    for item in selected:
        print(
            f"{match_number(item)} {league_name(item)} "
            f"{team_home(item)} vs {team_away(item)} "
            f"状态={item.get('matchStatus', item.get('matchResultStatus'))} 奖池={item.get('poolStatus', '')}"
        )
    print(f"Updated: {fixtures_path}")
    print(f"Updated: {ratings_path}")
    print(f"Updated: {odds_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
