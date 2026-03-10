"""
Agent Props Scout
-----------------
Fetches player stats from stats.nba.com via nba_api:
  - Season averages (LeagueDashPlayerStats) — one API call for all players
  - Recent game logs (PlayerGameLogs) — one API call, last N days
  - Computes L5/L10 ppg, minutes, std_dev from game_log
"""

import math
import time
from datetime import date, timedelta
from typing import Optional

from config.logging_config import setup_logger
from config.settings import (
    PROPS_SEASON,
    PROPS_RECENT_DAYS,
    PROPS_MIN_MINUTES,
    PROPS_MIN_GAMES,
    NBA_STATS_TIMEOUT,
    NBA_STATS_DELAY,
    NBA_TEAM_ABBREV,
)
from data.database import (
    upsert_player_stats,
    upsert_player_game_log,
    get_all_player_stats,
    get_player_game_log,
)

logger = setup_logger("AgentPropsScout")

# Required headers for stats.nba.com
NBA_STATS_HEADERS = {
    "Host": "stats.nba.com",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "Connection": "keep-alive",
}


def _parse_minutes(raw) -> Optional[float]:
    """Parse minutes value from nba_api — can be 'MM:SS' string or a float."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        raw = raw.strip()
        if ":" in raw:
            try:
                parts = raw.split(":")
                return float(parts[0]) + float(parts[1]) / 60.0
            except (ValueError, IndexError):
                return None
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def _parse_matchup(matchup: str) -> tuple[bool, str]:
    """
    Parse MATCHUP string from PlayerGameLogs.
    'LAL vs. GSW' → is_home=True, opponent='GSW'
    'LAL @ GSW'   → is_home=False, opponent='GSW'
    Returns (is_home, opponent_abbr).
    """
    matchup = matchup.strip()
    if " vs. " in matchup:
        parts = matchup.split(" vs. ")
        return True, parts[-1].strip()
    if " @ " in matchup:
        parts = matchup.split(" @ ")
        return False, parts[-1].strip()
    # Fallback: assume home, no opponent
    return True, ""


def _parse_game_date(raw_date: str) -> str:
    """Parse '2026-03-09T00:00:00' → '2026-03-09'."""
    if not raw_date:
        return ""
    return raw_date[:10]


def _nba_api_call(endpoint_cls, max_attempts: int = 3, **kwargs):
    """
    Retry wrapper for nba_api endpoint calls.
    Sleeps NBA_STATS_DELAY before each attempt, exponential backoff on failure.
    Returns raw dict from get_dict() or None on exhausted retries.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            time.sleep(NBA_STATS_DELAY)
            endpoint = endpoint_cls(
                headers=NBA_STATS_HEADERS,
                timeout=NBA_STATS_TIMEOUT,
                **kwargs,
            )
            return endpoint.get_dict()
        except Exception as exc:
            last_exc = exc
            wait = 2.0 ** attempt
            logger.warning(
                "nba_api call '%s' failed (attempt %d/%d): %s – retrying in %.1fs",
                endpoint_cls.__name__,
                attempt,
                max_attempts,
                exc,
                wait,
            )
            time.sleep(wait)
    logger.error(
        "nba_api call '%s' exhausted all %d retries. Last error: %s",
        endpoint_cls.__name__,
        max_attempts,
        last_exc,
    )
    return None


def _rows_to_dicts(data: dict, result_set_index: int = 0) -> list[dict]:
    """Convert nba_api resultSets structure to list of dicts."""
    try:
        rs = data["resultSets"][result_set_index]
        headers = rs["headers"]
        rows = rs["rowSet"]
        return [dict(zip(headers, row)) for row in rows]
    except (KeyError, IndexError, TypeError) as exc:
        logger.error("Failed to parse resultSets[%d]: %s", result_set_index, exc)
        return []


class AgentPropsScout:

    def fetch_and_store_season_stats(self) -> bool:
        """
        Fetches season averages for all NBA players via LeagueDashPlayerStats
        and upserts them into player_stats table.
        Filters out players with < PROPS_MIN_GAMES GP or < PROPS_MIN_MINUTES avg_min.
        """
        logger.info(
            "Fetching season stats for %s via LeagueDashPlayerStats", PROPS_SEASON
        )

        try:
            from nba_api.stats.endpoints import leaguedashplayerstats
        except ImportError:
            logger.error(
                "nba_api package not installed. Run: pip install nba_api>=1.4.1"
            )
            return False

        data = _nba_api_call(
            leaguedashplayerstats.LeagueDashPlayerStats,
            Season=PROPS_SEASON,
            SeasonType="Regular Season",
            PerMode="PerGame",
        )
        if data is None:
            logger.error("LeagueDashPlayerStats fetch returned None")
            return False

        players = _rows_to_dicts(data)
        logger.info("Received %d player rows from LeagueDashPlayerStats", len(players))

        stored = 0
        skipped = 0
        for p in players:
            try:
                gp = p.get("GP") or 0
                avg_min = _parse_minutes(p.get("MIN")) or 0.0
                if gp < PROPS_MIN_GAMES:
                    skipped += 1
                    continue
                if avg_min < PROPS_MIN_MINUTES:
                    skipped += 1
                    continue

                team_abbr = p.get("TEAM_ABBREVIATION", "")
                team_name = NBA_TEAM_ABBREV.get(team_abbr, team_abbr)

                stats = {
                    "player_id": str(p.get("PLAYER_ID", "")),
                    "player_name": p.get("PLAYER_NAME", ""),
                    "team_id": str(p.get("TEAM_ID", "")),
                    "team_name": team_name,
                    "season": PROPS_SEASON,
                    "games_played": gp,
                    "avg_minutes": round(avg_min, 2),
                    "avg_points": round(float(p.get("PTS") or 0), 2),
                    "avg_fga": round(float(p.get("FGA") or 0), 2),
                    "avg_fta": round(float(p.get("FTA") or 0), 2),
                    "avg_fg3a": round(float(p.get("FG3A") or 0), 2),
                    # l5/l10/std_dev will be populated by _recompute_recent_averages
                    "l5_ppg": None,
                    "l10_ppg": None,
                    "l5_minutes": None,
                    "l10_minutes": None,
                    "std_dev_points": None,
                }
                upsert_player_stats(stats)
                stored += 1
            except Exception as exc:
                logger.error(
                    "Error storing stats for player %s: %s",
                    p.get("PLAYER_NAME", "?"),
                    exc,
                )

        logger.info(
            "Season stats stored: %d players (skipped %d below thresholds)",
            stored,
            skipped,
        )
        return stored > 0

    def fetch_and_store_recent_logs(self) -> bool:
        """
        Fetches player game logs for the last PROPS_RECENT_DAYS days via
        PlayerGameLogs and upserts entries into player_game_log.
        Calls _recompute_recent_averages() after storing.
        """
        logger.info(
            "Fetching recent game logs (last %d days) for %s via PlayerGameLogs",
            PROPS_RECENT_DAYS,
            PROPS_SEASON,
        )

        try:
            from nba_api.stats.endpoints import playergamelogs
        except ImportError:
            logger.error(
                "nba_api package not installed. Run: pip install nba_api>=1.4.1"
            )
            return False

        date_from = (date.today() - timedelta(days=PROPS_RECENT_DAYS)).strftime(
            "%m/%d/%Y"
        )
        date_to = date.today().strftime("%m/%d/%Y")

        data = _nba_api_call(
            playergamelogs.PlayerGameLogs,
            Season=PROPS_SEASON,
            SeasonType="Regular Season",
            DateFrom=date_from,
            DateTo=date_to,
        )
        if data is None:
            logger.error("PlayerGameLogs fetch returned None")
            return False

        logs = _rows_to_dicts(data)
        logger.info(
            "Received %d game log rows from PlayerGameLogs (last %d days)",
            len(logs),
            PROPS_RECENT_DAYS,
        )

        stored = 0
        skipped = 0
        for row in logs:
            try:
                minutes = _parse_minutes(row.get("MIN")) or 0.0
                if minutes < PROPS_MIN_MINUTES:
                    skipped += 1
                    continue

                is_home, opponent_abbr = _parse_matchup(row.get("MATCHUP", ""))
                game_date = _parse_game_date(row.get("GAME_DATE", ""))

                team_abbr = row.get("TEAM_ABBREVIATION", "")
                team_name = NBA_TEAM_ABBREV.get(team_abbr, team_abbr)

                entry = {
                    "player_id": str(row.get("PLAYER_ID", "")),
                    "player_name": row.get("PLAYER_NAME", ""),
                    "team_id": str(row.get("TEAM_ID", "")),
                    "team_name": team_name,
                    "game_id": str(row.get("GAME_ID", "")),
                    "game_date": game_date,
                    "season": PROPS_SEASON,
                    "minutes": round(minutes, 2),
                    "points": int(row.get("PTS") or 0),
                    "fga": int(row.get("FGA") or 0),
                    "fgm": int(row.get("FGM") or 0),
                    "fg3a": int(row.get("FG3A") or 0),
                    "fg3m": int(row.get("FG3M") or 0),
                    "fta": int(row.get("FTA") or 0),
                    "ftm": int(row.get("FTM") or 0),
                    "opponent_abbr": opponent_abbr,
                    "is_home": 1 if is_home else 0,
                }
                upsert_player_game_log(entry)
                stored += 1
            except Exception as exc:
                logger.error(
                    "Error storing game log for player %s / game %s: %s",
                    row.get("PLAYER_NAME", "?"),
                    row.get("GAME_ID", "?"),
                    exc,
                )

        logger.info(
            "Recent game logs stored: %d entries (skipped %d below min_minutes)",
            stored,
            skipped,
        )

        if stored > 0:
            self._recompute_recent_averages()

        return stored > 0

    def _recompute_recent_averages(self) -> None:
        """
        For each player in player_stats, queries player_game_log (limit 20,
        filtered to minutes >= PROPS_MIN_MINUTES), then computes:
          - l5_ppg, l10_ppg, l5_minutes, l10_minutes
          - std_dev_points (sample std dev when n >= 5, else None)
        Updates player_stats with computed values.
        """
        logger.info("Recomputing recent averages (L5/L10/std_dev) for all players")

        all_stats = get_all_player_stats(PROPS_SEASON)
        if not all_stats:
            logger.warning("No player stats in DB to recompute averages for")
            return

        updated = 0
        for ps in all_stats:
            try:
                player_id = ps["player_id"]
                raw_logs = get_player_game_log(player_id, PROPS_SEASON, limit=20)

                # Filter to qualifying minutes
                logs = [
                    g
                    for g in raw_logs
                    if (g["minutes"] or 0.0) >= PROPS_MIN_MINUTES
                ]

                if not logs:
                    continue

                pts_list = [int(g["points"] or 0) for g in logs]
                min_list = [float(g["minutes"] or 0.0) for g in logs]

                l5_pts = pts_list[:5]
                l10_pts = pts_list[:10]
                l5_min = min_list[:5]
                l10_min = min_list[:10]

                l5_ppg = round(sum(l5_pts) / len(l5_pts), 2) if l5_pts else None
                l10_ppg = round(sum(l10_pts) / len(l10_pts), 2) if l10_pts else None
                l5_minutes = round(sum(l5_min) / len(l5_min), 2) if l5_min else None
                l10_minutes = (
                    round(sum(l10_min) / len(l10_min), 2) if l10_min else None
                )

                # Sample std dev (n >= 5 for meaningful estimate)
                std_dev: Optional[float] = None
                if len(pts_list) >= 5:
                    mean = sum(pts_list) / len(pts_list)
                    variance = sum((p - mean) ** 2 for p in pts_list) / len(pts_list)
                    std_dev = round(math.sqrt(variance), 2)

                update_stats = {
                    "player_id": player_id,
                    "player_name": ps["player_name"],
                    "team_id": ps["team_id"],
                    "team_name": ps["team_name"],
                    "season": PROPS_SEASON,
                    "games_played": ps["games_played"],
                    "avg_minutes": ps["avg_minutes"],
                    "avg_points": ps["avg_points"],
                    "avg_fga": ps["avg_fga"],
                    "avg_fta": ps["avg_fta"],
                    "avg_fg3a": ps["avg_fg3a"],
                    "l5_ppg": l5_ppg,
                    "l10_ppg": l10_ppg,
                    "l5_minutes": l5_minutes,
                    "l10_minutes": l10_minutes,
                    "std_dev_points": std_dev,
                }
                upsert_player_stats(update_stats)
                updated += 1
            except Exception as exc:
                logger.error(
                    "Error recomputing averages for player_id=%s: %s",
                    ps["player_id"],
                    exc,
                )

        logger.info(
            "Recomputed recent averages for %d / %d players",
            updated,
            len(all_stats),
        )
