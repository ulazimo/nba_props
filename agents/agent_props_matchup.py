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
from config.settings import PROPS_SEASON, PROPS_STD_DEV_FACTOR, PROPS_B2B_FACTOR
from data.database import get_all_team_stats, get_team_stats, get_last_game_date

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

        # ── 4. Projected points ───────────────────────────────────────────
        projected = base_ppg * pace_factor * def_factor
        projected = max(0.1, round(projected, 2))

        # ── 5. B2B penalty ────────────────────────────────────────────────
        b2b = False
        if game_date:
            try:
                last_date = get_last_game_date(team_name, game_date)
                if last_date:
                    delta = (
                        datetime.strptime(game_date, "%Y-%m-%d")
                        - datetime.strptime(last_date, "%Y-%m-%d")
                    ).days
                    if delta == 1:
                        b2b = True
                        projected = round(projected * PROPS_B2B_FACTOR, 2)
                        logger.info(
                            "  B2B penalty: %s projected reduced to %.1f (×%.2f)",
                            player_name, projected, PROPS_B2B_FACTOR,
                        )
            except Exception as exc:
                logger.debug("B2B check failed for %s: %s", player_name, exc)

        # ── 6. Std dev ────────────────────────────────────────────────────
        raw_std = player_stats["std_dev_points"]
        if raw_std is not None and raw_std > 0:
            std_dev = float(raw_std)
        else:
            std_dev = PROPS_STD_DEV_FACTOR * projected

        std_dev = round(std_dev, 2)

        games_used = player_stats["games_played"] or 0

        logger.info(
            "  %s: base=%.1f pace=%.3f def=%.3f projected=%.1f σ=%.1f%s",
            player_name,
            base_ppg,
            pace_factor,
            def_factor,
            projected,
            std_dev,
            " [B2B]" if b2b else "",
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
        )
