"""
Agent Props Mathematician
-------------------------
Computes over/under probabilities for player points using normal distribution.
σ = historical scoring std dev (or PROPS_STD_DEV_FACTOR × projected as fallback).
"""

import math
from typing import Optional

from config.logging_config import setup_logger
from agents.agent_props_matchup import PlayerMatchupReport

logger = setup_logger("AgentPropsMathematician")


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    """Normal CDF using error function."""
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2))))


class PlayerProbResult:

    def __init__(
        self,
        projected_points: float,
        points_line: float,
        over_prob: float,
        under_prob: float,
        sigma: float,
    ):
        self.projected_points = round(projected_points, 2)
        self.points_line = points_line
        self.over_prob = round(over_prob, 4)
        self.under_prob = round(under_prob, 4)
        self.sigma = round(sigma, 2)

    def to_dict(self) -> dict:
        return {
            "projected_points": self.projected_points,
            "points_line": self.points_line,
            "over_prob": self.over_prob,
            "under_prob": self.under_prob,
            "sigma": self.sigma,
        }


class AgentPropsMathematician:

    def calculate(
        self,
        report: PlayerMatchupReport,
        line: float,
    ) -> Optional[PlayerProbResult]:
        """
        Compute over/under probabilities for a player points line.

        Parameters
        ----------
        report : PlayerMatchupReport from AgentPropsMatchup
        line   : bookmaker's player points over/under line
        """
        projected = report.projected_points
        sigma = report.std_dev

        if projected <= 0:
            logger.error(
                "Invalid projected points %.2f for %s – cannot compute probabilities",
                projected,
                report.player_name,
            )
            return None

        if sigma <= 0:
            logger.warning(
                "Sigma=%.2f for %s – using 1.0 as fallback", sigma, report.player_name
            )
            sigma = 1.0

        # P(actual > line) using N(projected, sigma²)
        over_prob = 1.0 - _normal_cdf(line, projected, sigma)
        under_prob = _normal_cdf(line, projected, sigma)

        result = PlayerProbResult(
            projected_points=projected,
            points_line=line,
            over_prob=over_prob,
            under_prob=under_prob,
            sigma=sigma,
        )

        logger.info(
            "  %s: projected=%.1f σ=%.1f line=%.1f → P(over)=%.1f%% P(under)=%.1f%%",
            report.player_name,
            projected,
            sigma,
            line,
            over_prob * 100,
            under_prob * 100,
        )
        return result
