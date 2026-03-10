"""
Agent Matchup Expert (v3)
--------------------------
Analizira matchup koristeći:
  1. Elo ratings (sa home court adjustment)
  2. Opponent-adjusted expected scores (season + location splits)
  3. Recent form weighting (last 10 games, 30% weight)
  4. Head-to-head history (weight scales with sample size)
  5. Real pace blending
  6. Home court advantage (+2.5 pts empirical NBA average)

Čita isključivo iz baze – nema direktnih API poziva.
"""

from datetime import datetime
from typing import Optional

from config.logging_config import setup_logger
from config.settings import CURRENT_SEASON, HOME_COURT_PTS, B2B_PENALTY
from data.database import get_team_stats, get_all_team_stats, get_h2h_games, get_last_game_date

logger = setup_logger("AgentMatchupExpert")

SEASON_WEIGHT = 0.7
RECENT_WEIGHT = 0.3
H2H_WEIGHT_MAX = 0.10    # max H2H blending weight (reached at 3+ H2H games)
H2H_MIN_GAMES = 3        # games needed for full H2H weight


class MatchupReport:

    def __init__(
        self,
        home_team: str,
        away_team: str,
        home_expected_score: float,
        away_expected_score: float,
        home_elo: float,
        away_elo: float,
        elo_home_win_prob: float,
        expected_pace: float,
        home_court_adj: float,
        home_form: str,
        away_form: str,
        h2h_summary: str,
        confidence: float,
        breakdown: dict,
    ):
        self.home_team = home_team
        self.away_team = away_team
        self._home_expected_score = home_expected_score
        self._away_expected_score = away_expected_score
        self.home_elo = home_elo
        self.away_elo = away_elo
        self.elo_home_win_prob = elo_home_win_prob
        self.expected_pace = expected_pace
        self.home_court_adj = home_court_adj
        self.home_form = home_form
        self.away_form = away_form
        self.h2h_summary = h2h_summary
        self.confidence = confidence
        self.breakdown = breakdown

    @property
    def home_expected_score(self) -> float:
        return self._home_expected_score

    @property
    def away_expected_score(self) -> float:
        return self._away_expected_score

    @property
    def expected_total(self) -> float:
        return round(self.home_expected_score + self.away_expected_score, 2)

    def to_dict(self) -> dict:
        return {
            "home_team": self.home_team,
            "away_team": self.away_team,
            "home_expected_score": self.home_expected_score,
            "away_expected_score": self.away_expected_score,
            "expected_total": self.expected_total,
            "home_elo": self.home_elo,
            "away_elo": self.away_elo,
            "elo_home_win_prob": self.elo_home_win_prob,
            "expected_pace": self.expected_pace,
            "home_court_adj": self.home_court_adj,
            "home_form": self.home_form,
            "away_form": self.away_form,
            "h2h_summary": self.h2h_summary,
            "confidence": self.confidence,
            "breakdown": self.breakdown,
        }


class AgentMatchupExpert:

    def __init__(self):
        self._league_cache: Optional[dict] = None

    def invalidate_cache(self) -> None:
        """Force league averages to be recomputed on next analyze() call."""
        self._league_cache = None

    def _get_league_averages(self) -> dict:
        if self._league_cache is not None:
            return self._league_cache

        all_stats = get_all_team_stats(CURRENT_SEASON)
        if not all_stats:
            logger.warning("No team stats in DB – defaults used")
            return {"avg_pts": 115.0, "avg_pace": 100.0, "avg_opp": 115.0}

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

    def analyze(self, home_team: str, away_team: str, game_date: Optional[str] = None) -> Optional[MatchupReport]:
        logger.info("Analyzing matchup: %s (home) vs %s (away)", home_team, away_team)

        hs = get_team_stats(home_team, CURRENT_SEASON)
        aws = get_team_stats(away_team, CURRENT_SEASON)
        if hs is None:
            logger.error("No stats for home team '%s'", home_team)
            return None
        if aws is None:
            logger.error("No stats for away team '%s'", away_team)
            return None

        lg = self._get_league_averages()

        # ── 1. Pace blending ─────────────────────────────────────────────
        home_pace = hs["pace"] or lg["avg_pace"]
        away_pace = aws["pace"] or lg["avg_pace"]
        expected_pace = round((home_pace + away_pace) / 2, 1)
        pace_factor = expected_pace / lg["avg_pace"] if lg["avg_pace"] > 0 else 1.0

        # ── 2. Season-based expected scores (opponent-adjusted) ──────────
        avg_pts = lg["avg_pts"]

        home_ppg = hs["home_ppg"] or hs["pts_per_game"] or avg_pts
        away_opp = aws["away_opp_ppg"] or aws["opp_pts_per_game"] or avg_pts

        away_ppg = aws["away_ppg"] or aws["pts_per_game"] or avg_pts
        home_opp = hs["home_opp_ppg"] or hs["opp_pts_per_game"] or avg_pts

        if avg_pts > 0:
            season_home = (home_ppg / avg_pts) * (away_opp / avg_pts) * avg_pts * pace_factor
            season_away = (away_ppg / avg_pts) * (home_opp / avg_pts) * avg_pts * pace_factor
        else:
            season_home = home_ppg
            season_away = away_ppg

        # ── 3. Recent form adjustment ────────────────────────────────────
        home_l10_ppg = hs["last10_ppg"] or home_ppg
        home_l10_opp = hs["last10_opp_ppg"] or home_opp
        away_l10_ppg = aws["last10_ppg"] or away_ppg
        away_l10_opp = aws["last10_opp_ppg"] or away_opp

        if avg_pts > 0:
            recent_home = (home_l10_ppg / avg_pts) * (away_l10_opp / avg_pts) * avg_pts * pace_factor
            recent_away = (away_l10_ppg / avg_pts) * (home_l10_opp / avg_pts) * avg_pts * pace_factor
        else:
            recent_home = home_l10_ppg
            recent_away = away_l10_ppg

        # ── 4. Blended expected scores ───────────────────────────────────
        blended_home = SEASON_WEIGHT * season_home + RECENT_WEIGHT * recent_home
        blended_away = SEASON_WEIGHT * season_away + RECENT_WEIGHT * recent_away

        # ── 5. H2H adjustment (weight scales with sample size) ───────────
        h2h_home = get_h2h_games(home_team, away_team)
        h2h_summary = "No H2H data"
        if h2h_home:
            h2h_wins = sum(1 for g in h2h_home if g["team_score"] > g["opp_score"])
            h2h_avg_pts = sum(g["team_score"] for g in h2h_home) / len(h2h_home)
            h2h_avg_opp = sum(g["opp_score"] for g in h2h_home) / len(h2h_home)
            h2h_summary = f"{h2h_wins}W-{len(h2h_home)-h2h_wins}L (avg {h2h_avg_pts:.0f}-{h2h_avg_opp:.0f})"

            # Scale H2H weight by sample size — 1 game gets minimal weight
            h2h_w = H2H_WEIGHT_MAX * min(1.0, len(h2h_home) / H2H_MIN_GAMES)
            blended_home = (1 - h2h_w) * blended_home + h2h_w * h2h_avg_pts
            blended_away = (1 - h2h_w) * blended_away + h2h_w * h2h_avg_opp
            logger.info(
                "  H2H: %s | weight=%.2f (n=%d)", h2h_summary, h2h_w, len(h2h_home)
            )

        # ── 6. Home court advantage ──────────────────────────────────────
        blended_home += HOME_COURT_PTS / 2
        blended_away -= HOME_COURT_PTS / 2

        # ── 7. Back-to-back penalty ──────────────────────────────────────
        home_b2b = away_b2b = False
        if game_date:
            for team_name, is_home in ((home_team, True), (away_team, False)):
                last_date = get_last_game_date(team_name, game_date)
                if last_date:
                    delta = (
                        datetime.strptime(game_date, "%Y-%m-%d")
                        - datetime.strptime(last_date, "%Y-%m-%d")
                    ).days
                    if delta == 1:
                        logger.info("  B2B penalty: %s (-%.1f pts)", team_name, B2B_PENALTY)
                        if is_home:
                            home_b2b = True
                            blended_home -= B2B_PENALTY
                        else:
                            away_b2b = True
                            blended_away -= B2B_PENALTY

        blended_home = round(blended_home, 2)
        blended_away = round(blended_away, 2)

        # ── 8. Elo-based win probability ─────────────────────────────────
        home_elo = hs["elo_rating"] or 1500.0
        away_elo = aws["elo_rating"] or 1500.0
        elo_home_prob = 1.0 / (1.0 + 10.0 ** ((away_elo - (home_elo + 100)) / 400.0))

        # ── 8. Form strings ──────────────────────────────────────────────
        home_streak = hs["streak"] or 0
        away_streak = aws["streak"] or 0
        home_l10w = hs["last10_wins"] or 0
        home_l10l = hs["last10_losses"] or 0
        away_l10w = aws["last10_wins"] or 0
        away_l10l = aws["last10_losses"] or 0

        def _streak_str(streak: int) -> str:
            if streak > 0:
                return f"W{streak}"
            elif streak < 0:
                return f"L{abs(streak)}"
            return "–"

        home_form = f"{home_l10w}W-{home_l10l}L (streak {_streak_str(home_streak)})" + (" [B2B]" if home_b2b else "")
        away_form = f"{away_l10w}W-{away_l10l}L (streak {_streak_str(away_streak)})" + (" [B2B]" if away_b2b else "")

        # ── 9. Confidence score ──────────────────────────────────────────
        gp_min = min(hs["games_played"] or 0, aws["games_played"] or 0)
        confidence = min(1.0, gp_min / 40.0)

        breakdown = {
            "season_home": round(season_home, 1),
            "season_away": round(season_away, 1),
            "recent_home": round(recent_home, 1),
            "recent_away": round(recent_away, 1),
            "pace_factor": round(pace_factor, 3),
            "home_elo": home_elo,
            "away_elo": away_elo,
            "home_b2b": home_b2b,
            "away_b2b": away_b2b,
        }

        report = MatchupReport(
            home_team=home_team,
            away_team=away_team,
            home_expected_score=blended_home,
            away_expected_score=blended_away,
            home_elo=home_elo,
            away_elo=away_elo,
            elo_home_win_prob=round(elo_home_prob, 4),
            expected_pace=expected_pace,
            home_court_adj=HOME_COURT_PTS,
            home_form=home_form,
            away_form=away_form,
            h2h_summary=h2h_summary,
            confidence=round(confidence, 2),
            breakdown=breakdown,
        )

        logger.info(
            "  Scores → home: %.1f | away: %.1f | total: %.1f | pace: %.1f",
            report.home_expected_score, report.away_expected_score,
            report.expected_total, expected_pace,
        )
        logger.info(
            "  Elo: %s %.0f vs %s %.0f → P(home)=%.1f%%",
            home_team, home_elo, away_team, away_elo, elo_home_prob * 100,
        )
        logger.info(
            "  Form: %s %s | %s %s | H2H: %s",
            home_team, home_form, away_team, away_form, h2h_summary,
        )
        return report
