"""
Agent Scout (v3)
-----------
Povlači podatke sa NBA CDN-a i računa:
  - Pace (poboljšana aproksimacija: total_pts / 2.15)
  - Home/away splits
  - Recent form (last 10 games)
  - Elo ratings sa margin-of-victory korekcijom
  - Per-game log za H2H i trend analizu
  - Inkrementalno procesiranje (samo nove igre)
"""

import time
import functools
from datetime import date, datetime
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config.logging_config import setup_logger
from config.settings import (
    NBA_API_DELAY,
    NBA_API_RETRIES,
    NBA_API_BACKOFF,
    CURRENT_SEASON,
)
from data.database import (
    upsert_game,
    upsert_team_stats,
    update_game_result,
    upsert_game_log,
    get_processed_game_ids,
)

logger = setup_logger("AgentScout")

CDN_SCOREBOARD = "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"
CDN_SCHEDULE = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
CDN_BOXSCORE = "https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{game_id}.json"

CDN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/121.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
}

ELO_K = 20.0
ELO_HOME_ADVANTAGE = 100.0
ELO_DEFAULT = 1500.0

# Pace conversion: NBA avg ~113 pts/team → ~100 possessions → factor ~0.885
# Using 2.15 divisor: 226 total pts → 105 pace (realistic NBA average)
PACE_DIVISOR = 2.15
SCHEDULE_CACHE_TTL = 12 * 3600  # seconds; NBA schedule rarely changes intra-day


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(CDN_HEADERS)
    return session


def cdn_api_call(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        last_exc: Optional[Exception] = None
        for attempt in range(1, NBA_API_RETRIES + 1):
            try:
                time.sleep(NBA_API_DELAY)
                return func(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                wait = NBA_API_BACKOFF ** attempt
                logger.warning(
                    "CDN call '%s' failed (attempt %d/%d): %s – retrying in %.1fs",
                    func.__name__, attempt, NBA_API_RETRIES, exc, wait,
                )
                time.sleep(wait)
        logger.error(
            "CDN call '%s' exhausted all %d retries. Last error: %s",
            func.__name__, NBA_API_RETRIES, last_exc,
        )
        return None
    return wrapper


TEAM_FULL_NAMES = {
    "76ers": "Philadelphia 76ers", "Bucks": "Milwaukee Bucks",
    "Bulls": "Chicago Bulls", "Cavaliers": "Cleveland Cavaliers",
    "Celtics": "Boston Celtics", "Clippers": "LA Clippers",
    "Grizzlies": "Memphis Grizzlies", "Hawks": "Atlanta Hawks",
    "Heat": "Miami Heat", "Hornets": "Charlotte Hornets",
    "Jazz": "Utah Jazz", "Kings": "Sacramento Kings",
    "Knicks": "New York Knicks", "Lakers": "Los Angeles Lakers",
    "Magic": "Orlando Magic", "Mavericks": "Dallas Mavericks",
    "Nets": "Brooklyn Nets", "Nuggets": "Denver Nuggets",
    "Pacers": "Indiana Pacers", "Pelicans": "New Orleans Pelicans",
    "Pistons": "Detroit Pistons", "Raptors": "Toronto Raptors",
    "Rockets": "Houston Rockets", "Spurs": "San Antonio Spurs",
    "Suns": "Phoenix Suns", "Thunder": "Oklahoma City Thunder",
    "Timberwolves": "Minnesota Timberwolves",
    "Trail Blazers": "Portland Trail Blazers",
    "Warriors": "Golden State Warriors", "Wizards": "Washington Wizards",
}


def _full_name(team_name: str, team_city: str) -> str:
    return TEAM_FULL_NAMES.get(team_name, f"{team_city} {team_name}")


def _estimate_pace(home_score: int, away_score: int) -> float:
    """
    Estimate pace (possessions per 48 min) from final scores.
    NBA avg ~226 total pts → ~105 possessions.
    Using total / PACE_DIVISOR gives realistic range (90–115).
    """
    total = home_score + away_score
    return round(total / PACE_DIVISOR, 1) if total > 0 else 100.0


def _elo_expected(rating_a: float, rating_b: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))


def _mov_multiplier(winner_score: int, loser_score: int, winner_elo: float, loser_elo: float) -> float:
    """
    Margin-of-victory K multiplier (FiveThirtyEight NBA formula).
    Larger margins → more K, but diminishing returns.
    Elo difference correction prevents runaway ratings for dominant teams.
    """
    mov = winner_score - loser_score
    elo_diff = winner_elo - loser_elo
    return (mov + 3.0) ** 0.8 / (7.5 + 0.006 * elo_diff)


def _elo_update(
    winner_elo: float,
    loser_elo: float,
    winner_score: int,
    loser_score: int,
    k: float = ELO_K,
) -> tuple[float, float]:
    expected_w = _elo_expected(winner_elo, loser_elo)
    mov_mult = _mov_multiplier(winner_score, loser_score, winner_elo, loser_elo)
    delta = k * mov_mult * (1.0 - expected_w)
    new_winner = winner_elo + delta
    new_loser = loser_elo - delta
    return round(new_winner, 1), round(new_loser, 1)


class AgentScout:

    def __init__(self):
        self._session = _build_session()
        self._schedule_cache: Optional[list[dict]] = None
        self._schedule_cache_time: float = 0.0

    def fetch_todays_games(self, target_date: Optional[str] = None) -> list[dict]:
        game_date = target_date or date.today().strftime("%Y-%m-%d")
        logger.info("Fetching games for %s", game_date)

        games = self._fetch_from_schedule(game_date)
        if not games:
            logger.warning("No games found in schedule for %s", game_date)
            return []

        for g in games:
            upsert_game(g["game_id"], g["game_date"], g["home_team"], g["away_team"])
            logger.info("  Registered game %s: %s vs %s", g["game_id"], g["home_team"], g["away_team"])

        logger.info("Fetched %d games for %s", len(games), game_date)
        return games

    def fetch_and_store_team_stats(self) -> bool:
        """
        Incrementally processes only new completed games not yet in game_log.
        Rebuilds team_stats from the full game_log after inserting new entries.
        """
        logger.info("Computing team stats (incremental: pace, splits, Elo+MOV, form)")

        schedule = self._get_schedule()
        if schedule is None:
            logger.error("Failed to load schedule")
            return False

        scoreboard_games = self._fetch_scoreboard_raw()

        # Collect all completed games from schedule
        all_games: list[dict] = []
        for day_entry in schedule:
            game_date_str = day_entry.get("gameDate", "")
            for g in day_entry.get("games", []):
                if g.get("gameStatus", 0) == 3:
                    g["_parsed_date"] = game_date_str
                    all_games.append(g)

        # Add today's scoreboard games (already-final only, dedup by gameId)
        seen_in_schedule = {str(g.get("gameId", "")) for g in all_games}
        if scoreboard_games:
            for g in scoreboard_games:
                if g.get("gameStatus", 0) == 3:
                    gid = str(g.get("gameId", ""))
                    if gid not in seen_in_schedule:
                        g["_parsed_date"] = date.today().strftime("%m/%d/%Y 00:00:00")
                        all_games.append(g)

        if not all_games:
            logger.warning("No completed games found")
            return False

        # Sort chronologically so Elo is replayed in correct order
        def _game_date_key(g: dict):
            raw = g.get("_parsed_date", "")
            try:
                return datetime.strptime(raw[:10], "%m/%d/%Y")
            except (ValueError, IndexError):
                return datetime.min

        all_games.sort(key=_game_date_key)

        # ── Incremental: skip games already in game_log ───────────────────
        already_processed = get_processed_game_ids(CURRENT_SEASON)
        new_games = [g for g in all_games if str(g.get("gameId", "")) not in already_processed]
        logger.info(
            "Schedule: %d completed games total, %d already processed, %d new",
            len(all_games), len(already_processed), len(new_games),
        )

        # ── Process new games chronologically ─────────────────────────────
        new_count = 0
        for g in new_games:
            try:
                gid = str(g.get("gameId", ""))
                ht, at = g["homeTeam"], g["awayTeam"]
                home_score = ht.get("score", 0) or 0
                away_score = at.get("score", 0) or 0
                if home_score == 0 and away_score == 0:
                    continue

                pace = _estimate_pace(home_score, away_score)

                raw_date = g.get("_parsed_date", "")
                try:
                    gd = datetime.strptime(raw_date[:10], "%m/%d/%Y").strftime("%Y-%m-%d")
                except (ValueError, IndexError):
                    gd = date.today().strftime("%Y-%m-%d")

                for team, opp, t_score, o_score, is_home in [
                    (ht, at, home_score, away_score, True),
                    (at, ht, away_score, home_score, False),
                ]:
                    t_name = _full_name(team["teamName"], team["teamCity"])
                    o_name = _full_name(opp["teamName"], opp["teamCity"])
                    upsert_game_log({
                        "game_id": gid, "game_date": gd,
                        "team_id": str(team["teamId"]), "team_name": t_name,
                        "opponent_id": str(opp["teamId"]), "opponent_name": o_name,
                        "is_home": 1 if is_home else 0,
                        "team_score": t_score, "opp_score": o_score,
                        "pace": pace, "season": CURRENT_SEASON,
                    })
                new_count += 1
            except Exception as exc:
                logger.error("Error inserting game log for game %s: %s", g.get("gameId"), exc)

        logger.info("Inserted %d new game log entries", new_count)

        # ── Rebuild team_stats from full game_log ─────────────────────────
        return self._rebuild_team_stats_from_log(all_games)

    def _rebuild_team_stats_from_log(self, all_games: list[dict]) -> bool:
        """
        Recomputes all team_stats by replaying game_log in chronological order.
        Elo is computed from scratch each time to ensure correct sequential updates.
        """
        team_data: dict[int, dict] = {}
        elo_ratings: dict[int, float] = {}
        seen_game_ids: set[str] = set()

        for g in all_games:
            try:
                gid = g.get("gameId", "")
                if gid in seen_game_ids:
                    continue
                seen_game_ids.add(gid)

                ht, at = g["homeTeam"], g["awayTeam"]
                home_id, away_id = ht["teamId"], at["teamId"]
                home_score = ht.get("score", 0) or 0
                away_score = at.get("score", 0) or 0
                if home_score == 0 and away_score == 0:
                    continue

                pace = _estimate_pace(home_score, away_score)

                for team, opp_score, is_home in [
                    (ht, away_score, True), (at, home_score, False),
                ]:
                    tid = team["teamId"]
                    my_score = team.get("score", 0) or 0
                    if tid not in team_data:
                        team_data[tid] = {
                            "team_name": _full_name(team["teamName"], team["teamCity"]),
                            "gp": 0, "total_pts": 0, "total_opp": 0,
                            "home_gp": 0, "home_pts": 0, "home_opp": 0,
                            "away_gp": 0, "away_pts": 0, "away_opp": 0,
                            "home_w": 0, "home_l": 0, "away_w": 0, "away_l": 0,
                            "wins": 0, "losses": 0, "pace_sum": 0.0,
                            "recent": [],
                        }
                    td = team_data[tid]
                    td["gp"] += 1
                    td["total_pts"] += my_score
                    td["total_opp"] += opp_score
                    td["pace_sum"] += pace
                    won = my_score > opp_score
                    if won:
                        td["wins"] += 1
                    else:
                        td["losses"] += 1
                    if is_home:
                        td["home_gp"] += 1
                        td["home_pts"] += my_score
                        td["home_opp"] += opp_score
                        if won: td["home_w"] += 1
                        else:   td["home_l"] += 1
                    else:
                        td["away_gp"] += 1
                        td["away_pts"] += my_score
                        td["away_opp"] += opp_score
                        if won: td["away_w"] += 1
                        else:   td["away_l"] += 1
                    td["recent"].append({"pts": my_score, "opp": opp_score, "won": won})

                # Elo update with margin-of-victory correction
                h_elo = elo_ratings.get(home_id, ELO_DEFAULT)
                a_elo = elo_ratings.get(away_id, ELO_DEFAULT)
                if home_score > away_score:
                    h_elo, a_elo = _elo_update(h_elo, a_elo, home_score, away_score)
                else:
                    a_elo, h_elo = _elo_update(a_elo, h_elo, away_score, home_score)
                elo_ratings[home_id] = h_elo
                elo_ratings[away_id] = a_elo

            except Exception as exc:
                logger.error("Error processing game %s for stats: %s", g.get("gameId"), exc)

        count = 0
        for tid, td in team_data.items():
            gp = td["gp"]
            if gp == 0:
                continue

            ppg = td["total_pts"] / gp
            opp_ppg = td["total_opp"] / gp
            pace = td["pace_sum"] / gp

            off_rating = round(ppg / pace * 100, 1) if pace > 0 else 0.0
            def_rating = round(opp_ppg / pace * 100, 1) if pace > 0 else 0.0

            home_ppg = td["home_pts"] / td["home_gp"] if td["home_gp"] > 0 else ppg
            away_ppg = td["away_pts"] / td["away_gp"] if td["away_gp"] > 0 else ppg
            home_opp = td["home_opp"] / td["home_gp"] if td["home_gp"] > 0 else opp_ppg
            away_opp = td["away_opp"] / td["away_gp"] if td["away_gp"] > 0 else opp_ppg

            last10 = td["recent"][-10:]
            l10_ppg = sum(g["pts"] for g in last10) / len(last10) if last10 else ppg
            l10_opp = sum(g["opp"] for g in last10) / len(last10) if last10 else opp_ppg
            l10_w = sum(1 for g in last10 if g["won"])
            l10_l = len(last10) - l10_w

            streak = 0
            for g in reversed(td["recent"]):
                if g["won"]:
                    if streak >= 0: streak += 1
                    else: break
                else:
                    if streak <= 0: streak -= 1
                    else: break

            stats = {
                "team_id": str(tid), "team_name": td["team_name"],
                "season": CURRENT_SEASON, "games_played": gp,
                "off_rating": off_rating, "def_rating": def_rating,
                "pace": round(pace, 1),
                "pts_per_game": round(ppg, 1), "opp_pts_per_game": round(opp_ppg, 1),
                "home_ppg": round(home_ppg, 1), "away_ppg": round(away_ppg, 1),
                "home_opp_ppg": round(home_opp, 1), "away_opp_ppg": round(away_opp, 1),
                "home_wins": td["home_w"], "home_losses": td["home_l"],
                "away_wins": td["away_w"], "away_losses": td["away_l"],
                "last10_ppg": round(l10_ppg, 1), "last10_opp_ppg": round(l10_opp, 1),
                "last10_wins": l10_w, "last10_losses": l10_l,
                "elo_rating": elo_ratings.get(tid, ELO_DEFAULT),
                "streak": streak,
            }
            upsert_team_stats(stats)
            count += 1

        logger.info("Rebuilt stats for %d teams (Elo+MOV, improved pace)", count)
        return count > 0

    def fetch_game_results(self, game_ids: list[str]) -> int:
        updated = 0
        scoreboard_games = self._fetch_scoreboard_raw()
        if scoreboard_games is None:
            scoreboard_games = []
        sb_lookup = {g["gameId"]: g for g in scoreboard_games}

        for game_id in game_ids:
            logger.info("Fetching result for game %s", game_id)
            g = sb_lookup.get(game_id)
            if g and g.get("gameStatus") == 3:
                home_score = g["homeTeam"]["score"]
                away_score = g["awayTeam"]["score"]
                update_game_result(game_id, home_score, away_score)
                logger.info("  Game %s result: home=%d away=%d", game_id, home_score, away_score)
                updated += 1
                continue

            result = self._fetch_boxscore(game_id)
            if result:
                if result.get("gameStatus") != 3:
                    logger.warning(
                        "  Game %s not yet final (status=%s) – skipping",
                        game_id, result.get("gameStatus"),
                    )
                    continue
                home_score = result["homeTeam"]["score"]
                away_score = result["awayTeam"]["score"]
                update_game_result(game_id, home_score, away_score)
                logger.info("  Game %s result: home=%d away=%d", game_id, home_score, away_score)
                updated += 1
            else:
                logger.warning("Could not fetch result for game %s", game_id)
        return updated

    # ── CDN fetchers ──────────────────────────────────────────────────────

    @cdn_api_call
    def _fetch_scoreboard_raw(self) -> Optional[list[dict]]:
        resp = self._session.get(CDN_SCOREBOARD, timeout=15)
        resp.raise_for_status()
        return resp.json()["scoreboard"]["games"]

    @cdn_api_call
    def _fetch_boxscore(self, game_id: str) -> Optional[dict]:
        url = CDN_BOXSCORE.format(game_id=game_id)
        resp = self._session.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json().get("game")

    def _get_schedule(self) -> Optional[list[dict]]:
        age = time.time() - self._schedule_cache_time
        if self._schedule_cache is not None and age < SCHEDULE_CACHE_TTL:
            return self._schedule_cache
        data = self._fetch_schedule_raw()
        if data is not None:
            self._schedule_cache = data
            self._schedule_cache_time = time.time()
        return data

    @cdn_api_call
    def _fetch_schedule_raw(self) -> Optional[list[dict]]:
        resp = self._session.get(CDN_SCHEDULE, timeout=30)
        resp.raise_for_status()
        return resp.json()["leagueSchedule"]["gameDates"]

    def _fetch_from_schedule(self, game_date: str) -> list[dict]:
        schedule = self._get_schedule()
        if schedule is None:
            return []
        try:
            dt = datetime.strptime(game_date, "%Y-%m-%d")
            search_date = dt.strftime("%m/%d/%Y")
        except ValueError:
            search_date = game_date

        games = []
        for day_entry in schedule:
            if search_date in day_entry.get("gameDate", ""):
                for g in day_entry.get("games", []):
                    try:
                        ht, at = g["homeTeam"], g["awayTeam"]
                        games.append({
                            "game_id": str(g["gameId"]),
                            "game_date": game_date,
                            "home_team": _full_name(ht["teamName"], ht["teamCity"]),
                            "away_team": _full_name(at["teamName"], at["teamCity"]),
                        })
                    except Exception as exc:
                        logger.error("Error parsing schedule game: %s", exc)
                break
        return games
