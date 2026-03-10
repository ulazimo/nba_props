"""
Agent Mathematician (v3)
------------------------
Koristi normalnu distribuciju za score-based verovatnoće i blenduje
sa Elo verovatnoćama iz MatchupReport-a za finalni output.

  Win prob  = score_w * score_based + elo_w * elo_based
              gdje score_w + elo_w = 1.0 (elo_w skalira sa confidence)

  Sigma modela:
    - Svaki tim ima svoju sigmu baziranu na tempu
    - diff_sigma  = sqrt(home_sigma² + away_sigma²)  za win prob
    - total_sigma = sqrt(home_sigma² + away_sigma²)  za O/U (isti, ali konceptualno odvojen)
"""

import math
from typing import Optional

from config.logging_config import setup_logger
from config.settings import BASE_STD_DEV, SCORE_WEIGHT, ELO_WEIGHT
from agents.agent_matchup_expert import MatchupReport

logger = setup_logger("AgentMathematician")


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    """Normal CDF using error function."""
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def _team_std_dev(pace: float) -> float:
    """Per-team score std dev, scaled by pace."""
    return BASE_STD_DEV * (pace / 100.0) if pace > 0 else BASE_STD_DEV


class ProbabilityResult:
    def __init__(
        self,
        home_win_prob: float,
        away_win_prob: float,
        predicted_home_score: float,
        predicted_away_score: float,
        over_prob: float,
        under_prob: float,
        total_line: float,
        diff_sigma: float,
        total_sigma: float,
    ):
        self.home_win_prob = round(home_win_prob, 4)
        self.away_win_prob = round(away_win_prob, 4)
        self.predicted_home_score = round(predicted_home_score, 2)
        self.predicted_away_score = round(predicted_away_score, 2)
        self.predicted_total = round(predicted_home_score + predicted_away_score, 2)
        self.over_prob = round(over_prob, 4)
        self.under_prob = round(under_prob, 4)
        self.total_line = total_line
        self.diff_sigma = round(diff_sigma, 2)
        self.total_sigma = round(total_sigma, 2)

    def to_dict(self) -> dict:
        return {
            "home_win_prob": self.home_win_prob,
            "away_win_prob": self.away_win_prob,
            "predicted_home_score": self.predicted_home_score,
            "predicted_away_score": self.predicted_away_score,
            "predicted_total": self.predicted_total,
            "over_prob": self.over_prob,
            "under_prob": self.under_prob,
            "total_line": self.total_line,
            "diff_sigma": self.diff_sigma,
            "total_sigma": self.total_sigma,
        }


class AgentMathematician:

    def calculate(
        self,
        report: MatchupReport,
        total_line: Optional[float] = None,
    ) -> Optional[ProbabilityResult]:
        """
        Parameters
        ----------
        report     : MatchupReport from AgentMatchupExpert
        total_line : bookmaker's over/under line (if available)
        """
        home_exp = report.home_expected_score
        away_exp = report.away_expected_score

        if home_exp <= 0 or away_exp <= 0:
            logger.error(
                "Invalid expected scores: home=%.2f away=%.2f – cannot compute probabilities",
                home_exp, away_exp,
            )
            return None

        logger.info(
            "Computing probabilities: home=%.1f away=%.1f (Elo P(home)=%.1f%%)",
            home_exp, away_exp,
            getattr(report, 'elo_home_win_prob', 0.5) * 100,
        )

        pace = getattr(report, 'expected_pace', 100.0) or 100.0

        # Per-team sigma based on pace
        sigma = _team_std_dev(pace)

        # Score difference distribution: N(diff_mu, diff_sigma²)
        # diff_sigma = sqrt(sigma_home² + sigma_away²)
        # Assuming equal variance per team: sqrt(2) * sigma
        diff_mu = home_exp - away_exp
        diff_sigma = math.sqrt(sigma ** 2 + sigma ** 2)  # = sigma * sqrt(2)

        score_home_win = 1.0 - _normal_cdf(0, diff_mu, diff_sigma)
        score_away_win = _normal_cdf(0, diff_mu, diff_sigma)

        # Elo-based win probability
        elo_home_prob = getattr(report, 'elo_home_win_prob', None)

        # Blend: confidence scales Elo weight; score absorbs remainder
        if elo_home_prob is not None and 0.01 < elo_home_prob < 0.99:
            confidence = getattr(report, 'confidence', 1.0) or 1.0
            elo_w = ELO_WEIGHT * confidence
            score_w = SCORE_WEIGHT + (ELO_WEIGHT - elo_w)
            home_win = score_w * score_home_win + elo_w * elo_home_prob
            away_win = 1.0 - home_win
            logger.info(
                "  Blended: score(w=%.2f) P(home)=%.1f%% + Elo(w=%.2f) P(home)=%.1f%% "
                "→ final P(home)=%.1f%% (confidence=%.2f)",
                score_w, score_home_win * 100,
                elo_w, elo_home_prob * 100,
                home_win * 100, confidence,
            )
        else:
            home_win = score_home_win
            away_win = score_away_win

        # Over/Under: total score distribution N(total_mu, total_sigma²)
        # total_sigma = sqrt(sigma_home² + sigma_away²) — same formula, independent teams
        ou_line = total_line if total_line else (home_exp + away_exp)
        total_mu = home_exp + away_exp
        total_sigma = math.sqrt(sigma ** 2 + sigma ** 2)  # = diff_sigma (equal variance)

        over_prob = 1.0 - _normal_cdf(ou_line, total_mu, total_sigma)
        under_prob = _normal_cdf(ou_line, total_mu, total_sigma)

        result = ProbabilityResult(
            home_win_prob=home_win,
            away_win_prob=away_win,
            predicted_home_score=home_exp,
            predicted_away_score=away_exp,
            over_prob=over_prob,
            under_prob=under_prob,
            total_line=ou_line,
            diff_sigma=diff_sigma,
            total_sigma=total_sigma,
        )

        logger.info(
            "  P(home win)=%.2f%% | P(away win)=%.2f%% | "
            "P(over %.1f)=%.2f%% | predicted total=%.1f | σ=%.1f",
            result.home_win_prob * 100,
            result.away_win_prob * 100,
            ou_line,
            result.over_prob * 100,
            result.predicted_total,
            sigma,
        )
        return result
