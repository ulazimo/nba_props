"""
Agent Props Matchup
-------------------
Projects player points for a specific game:
  1. Base PPG: 0.6 × season_ppg + 0.4 × l10_ppg (falls back to season_ppg if no l10)
  2. Pace factor: game_pace / league_avg_pace
  3. Defensive factor: opp_pts_per_game_allowed / league_avg_pts_allowed
  projected = base × pace_factor × def_factor
"""

import sqlite3
from datetime import datetime
from typing import Optional

from config.logging_config import setup_logger
from config.settings import (
    PROPS_SEASON,
    PROPS_STD_DEV_FACTOR,
    PROPS_B2B_FACTOR,
    PROPS_REST_BOOST,
    NBA_NAME_TO_ABBREV,
)
from data.database import (
    get_all_team_stats,
    get_team_stats,
    get_last_game_date,
    get_player_home_away_ppg,
    get_player_vs_opponent,
)

logger = setup_logger("AgentPropsMatchup")


class PlayerMatchupReport:

    def __init__(
        self,
        player_name: str,
        team_name: str,
        opponent_team: str,
        projected_points: float,
        std_dev: float,
        base_ppg: float,
        pace_factor: float,
        def_factor: float,
        games_used: int,
        b2b: bool = False,
        rest_days: Optional[int] = None,
        ha_factor: float = 1.0,
        h2h_factor: float = 1.0,
        trend_factor: float = 1.0,
    ):
        self.player_name = player_name
        self.team_name = team_name
        self.opponent_team = opponent_team
        self.projected_points = projected_points
        self.std_dev = std_dev
        self.base_ppg = base_ppg
        self.pace_factor = pace_factor
        self.def_factor = def_factor
        self.games_used = games_used
        self.b2b = b2b
        self.rest_days = rest_days
        self.ha_factor = ha_factor
        self.h2h_factor = h2h_factor
        self.trend_factor = trend_factor

    def to_dict(self) -> dict:
        return {
            "player_name": self.player_name,
            "team_name": self.team_name,
            "opponent_team": self.opponent_team,
            "projected_points": self.projected_points,
            "std_dev": self.std_dev,
            "base_ppg": self.base_ppg,
            "pace_factor": self.pace_factor,
            "def_factor": self.def_factor,
            "games_used": self.games_used,
            "b2b": self.b2b,
            "rest_days": self.rest_days,
            "ha_factor": self.ha_factor,
            "h2h_factor": self.h2h_factor,
            "trend_factor": self.trend_factor,
        }


class AgentPropsMatchup:

    def __init__(self):
        self._league_cache: Optional[dict] = None

    def _get_league_averages(self) -> dict:
        if self._league_cache is not None:
            return self._league_cache

        all_stats = get_all_team_stats(PROPS_SEASON)
        if not all_stats:
            logger.warning("No team stats in DB – defaults used for league averages")
            return {"avg_pts": 115.0, "avg_opp": 115.0, "avg_pace": 100.0}

        qualified = [r for r in all_stats if (r["games_played"] or 0) >= 20]
        if not qualified:
            qualified = all_stats

        pts = [r["pts_per_game"] for r in qualified if r["pts_per_game"]]
        opp = [r["opp_pts_per_game"] for r in qualified if r["opp_pts_per_game"]]
        paces = [r["pace"] for r in qualified if r["pace"]]

        self._league_cache = {
            "avg_pts": sum(pts) / len(pts) if pts else 115.0,
            "avg_opp": sum(opp) / len(opp) if opp else 115.0,
            "avg_pace": sum(paces) / len(paces) if paces else 100.0,
        }
        logger.info(
            "League averages: PPG=%.1f OPP=%.1f PACE=%.1f (%d teams)",
            self._league_cache["avg_pts"],
            self._league_cache["avg_opp"],
            self._league_cache["avg_pace"],
            len(qualified),
        )
        return self._league_cache

    def analyze(
        self,
        player_stats: sqlite3.Row,
        home_team: str,
        away_team: str,
        is_home: bool,
        game_date: Optional[str] = None,
    ) -> Optional[PlayerMatchupReport]:
        """
        Projects player points for the given game context.

        Parameters
        ----------
        player_stats : Row from player_stats table
        home_team    : Full name of the home team
        away_team    : Full name of the away team
        is_home      : Whether this player's team is the home team
        """
        player_name = player_stats["player_name"]
        team_name = player_stats["team_name"] or (home_team if is_home else away_team)
        opponent_team = away_team if is_home else home_team

        logger.info(
            "Analyzing props matchup: %s (%s) vs %s",
            player_name,
            team_name,
            opponent_team,
        )

        lg = self._get_league_averages()

        # ── 1. Base PPG: blend season avg with L10 and L5 ────────────────
        season_ppg = player_stats["avg_points"] or 0.0
        l10_ppg = player_stats["l10_ppg"]
        l5_ppg = player_stats["l5_ppg"]

        if l5_ppg is not None and l10_ppg is not None:
            # Full blend: season anchors, L10 medium-term, L5 most recent form
            base_ppg = 0.4 * season_ppg + 0.3 * l10_ppg + 0.3 * l5_ppg
        elif l10_ppg is not None:
            base_ppg = 0.6 * season_ppg + 0.4 * l10_ppg
        elif l5_ppg is not None:
            base_ppg = 0.7 * season_ppg + 0.3 * l5_ppg
        else:
            base_ppg = season_ppg

        if base_ppg <= 0:
            logger.warning(
                "Player %s has base_ppg=%.2f – skipping", player_name, base_ppg
            )
            return None

        # ── 2. Pace factor ────────────────────────────────────────────────
        home_stats = get_team_stats(home_team, PROPS_SEASON)
        away_stats = get_team_stats(away_team, PROPS_SEASON)

        home_pace = (home_stats["pace"] if home_stats and home_stats["pace"] else lg["avg_pace"])
        away_pace = (away_stats["pace"] if away_stats and away_stats["pace"] else lg["avg_pace"])
        game_pace = (home_pace + away_pace) / 2.0
        pace_factor = game_pace / lg["avg_pace"] if lg["avg_pace"] > 0 else 1.0

        # ── 3. Defensive factor ───────────────────────────────────────────
        opp_stats = get_team_stats(opponent_team, PROPS_SEASON)
        if opp_stats and opp_stats["opp_pts_per_game"]:
            opp_ppg_allowed = opp_stats["opp_pts_per_game"]
        else:
            opp_ppg_allowed = lg["avg_opp"]

        league_avg_opp = lg["avg_opp"]
        def_factor = opp_ppg_allowed / league_avg_opp if league_avg_opp > 0 else 1.0

        # ── 4. Home/Away PPG adjustment ───────────────────────────────────
        player_id = str(player_stats["player_id"])
        ha_factor = 1.0
        try:
            ha = get_player_home_away_ppg(player_id, PROPS_SEASON)
            location_ppg = ha["home_ppg"] if is_home else ha["away_ppg"]
            location_games = ha["home_games"] if is_home else ha["away_games"]
            if location_ppg and location_games >= 5 and season_ppg > 0:
                ha_factor = max(0.88, min(1.12, location_ppg / season_ppg))
                logger.debug(
                    "  %s H/A factor: %.3f (%s avg=%.1f, n=%d)",
                    player_name, ha_factor,
                    "home" if is_home else "away", location_ppg, location_games,
                )
        except Exception as exc:
            logger.debug("H/A lookup failed for %s: %s", player_name, exc)

        # ── 5. H2H vs opponent adjustment ────────────────────────────────
        h2h_factor = 1.0
        try:
            opp_abbr = NBA_NAME_TO_ABBREV.get(opponent_team, "")
            if opp_abbr:
                h2h_games = get_player_vs_opponent(player_id, opp_abbr, limit=6)
                if len(h2h_games) >= 3 and season_ppg > 0:
                    h2h_avg = sum(int(g["points"] or 0) for g in h2h_games) / len(h2h_games)
                    h2h_factor = max(0.80, min(1.20, h2h_avg / season_ppg))
                    logger.debug(
                        "  %s H2H vs %s: avg=%.1f → factor=%.3f (n=%d)",
                        player_name, opp_abbr, h2h_avg, h2h_factor, len(h2h_games),
                    )
        except Exception as exc:
            logger.debug("H2H lookup failed for %s: %s", player_name, exc)

        # ── 6. Rest days (B2B / extra rest) ──────────────────────────────
        b2b = False
        rest_days: Optional[int] = None
        rest_factor = 1.0
        if game_date:
            try:
                last_date = get_last_game_date(team_name, game_date)
                if last_date:
                    rest_days = (
                        datetime.strptime(game_date, "%Y-%m-%d")
                        - datetime.strptime(last_date, "%Y-%m-%d")
                    ).days
                    if rest_days == 1:
                        b2b = True
                        rest_factor = PROPS_B2B_FACTOR
                        logger.info("  B2B: %s (×%.2f)", player_name, PROPS_B2B_FACTOR)
                    elif rest_days >= 3:
                        rest_factor = PROPS_REST_BOOST
                        logger.debug(
                            "  Rest boost: %s (%d days, ×%.2f)",
                            player_name, rest_days, PROPS_REST_BOOST,
                        )
            except Exception as exc:
                logger.debug("Rest days check failed for %s: %s", player_name, exc)

        # ── 7. Scoring trend ─────────────────────────────────────────────
        trend_factor = 1.0
        if l5_ppg and l10_ppg and l10_ppg > 0:
            trend = l5_ppg / l10_ppg
            # ±5% max; 30% of the trend signal transferred to projection
            trend_factor = max(0.95, min(1.05, 1.0 + (trend - 1.0) * 0.3))
            logger.debug(
                "  %s trend: l5=%.1f l10=%.1f ratio=%.2f → factor=%.3f",
                player_name, l5_ppg, l10_ppg, trend, trend_factor,
            )

        # ── 8. Final projection ───────────────────────────────────────────
        projected = (
            base_ppg
            * pace_factor
            * def_factor
            * ha_factor
            * h2h_factor
            * rest_factor
            * trend_factor
        )
        projected = max(0.1, round(projected, 2))

        # ── 9. Std dev ────────────────────────────────────────────────────
        raw_std = player_stats["std_dev_points"]
        if raw_std is not None and raw_std > 0:
            std_dev = float(raw_std)
        else:
            std_dev = PROPS_STD_DEV_FACTOR * projected

        std_dev = round(std_dev, 2)

        games_used = player_stats["games_played"] or 0

        logger.info(
            "  %s: base=%.1f pace=%.3f def=%.3f ha=%.3f h2h=%.3f "
            "rest=%.3f trend=%.3f → proj=%.1f σ=%.1f%s",
            player_name, base_ppg, pace_factor, def_factor,
            ha_factor, h2h_factor, rest_factor, trend_factor,
            projected, std_dev,
            " [B2B]" if b2b else (f" [{rest_days}d rest]" if rest_days and rest_days >= 3 else ""),
        )

        return PlayerMatchupReport(
            player_name=player_name,
            team_name=team_name,
            opponent_team=opponent_team,
            projected_points=projected,
            std_dev=std_dev,
            base_ppg=round(base_ppg, 2),
            pace_factor=round(pace_factor, 3),
            def_factor=round(def_factor, 3),
            games_used=games_used,
            b2b=b2b,
            rest_days=rest_days,
            ha_factor=round(ha_factor, 3),
            h2h_factor=round(h2h_factor, 3),
            trend_factor=round(trend_factor, 3),
        )
