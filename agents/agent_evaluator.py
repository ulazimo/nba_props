"""
Agent Evaluator
----------------
Svako jutro proverava rezultate prethodne noći, upoređuje ih sa
predikcijama i ažurira profitabilnost u bazi.

Generiše dnevni performance report koji se loguje i čuva.
"""

from datetime import date, timedelta
from typing import Optional

from config.logging_config import setup_logger
from data.database import (
    get_finished_games_without_evaluation,
    get_predictions_for_game,
    save_bet_result,
    get_profitability_summary,
    get_calibration_data,
)

logger = setup_logger("AgentEvaluator")


class AgentEvaluator:

    def run_daily_evaluation(self) -> dict:
        """
        Main entry point – evaluates all finished games that have predictions
        but no bet_result yet.
        Returns a summary dict with performance metrics.
        """
        logger.info("=" * 60)
        logger.info("Starting daily evaluation – %s", date.today().isoformat())
        logger.info("=" * 60)

        pending_games = get_finished_games_without_evaluation()
        logger.info("Found %d game(s) pending evaluation", len(pending_games))

        evaluated = 0
        total_profit = 0.0

        for game in pending_games:
            pl = self._evaluate_game(game)
            if pl is not None:
                total_profit += pl
                evaluated += 1

        summary = get_profitability_summary()
        self._log_summary(summary, evaluated, total_profit)
        self._log_calibration()
        return summary

    # ── Private helpers ───────────────────────────────────────────────────

    def _evaluate_game(self, game) -> Optional[float]:
        game_id = game["game_id"]
        home_score = game["home_score"]
        away_score = game["away_score"]

        if home_score is None or away_score is None:
            logger.warning("Game %s has no final score – skipping", game_id)
            return None

        predictions = get_predictions_for_game(game_id)
        if not predictions:
            logger.warning("No predictions found for game %s", game_id)
            return None

        # Use the most recent prediction
        pred = predictions[0]
        pred_id = pred["id"]
        recommended_bet = pred["recommended_bet"]
        bet_stake = pred["bet_stake"] or 1.0

        if not recommended_bet:
            logger.info("Game %s had no recommended bet – skipping evaluation", game_id)
            return None

        actual_winner = "home_win" if home_score > away_score else "away_win"
        actual_total = home_score + away_score

        # Only use the bookmaker's total line for O/U evaluation.
        # predicted_total is the model output and must NOT be used as a line.
        total_line = pred["total_line"]  # None if no bookmaker odds were available

        outcome, bet_odds = self._determine_outcome(
            recommended_bet=recommended_bet,
            actual_winner=actual_winner,
            actual_total=actual_total,
            total_line=total_line,
            pred=pred,
        )

        profit_loss = self._calculate_pnl(outcome, bet_odds, bet_stake)

        result_record = {
            "prediction_id": pred_id,
            "game_id": game_id,
            "bet_type": recommended_bet,
            "bet_odds": bet_odds,
            "stake": bet_stake,
            "outcome": outcome,
            "profit_loss": profit_loss,
            "notes": (
                f"Final: {game['home_team']} {home_score} – "
                f"{away_score} {game['away_team']}"
            ),
        }
        save_bet_result(result_record)

        emoji = "✓" if outcome == "win" else "✗"
        logger.info(
            "%s Game %s | bet=%s @ %.2f | stake=%.1f%% | P&L=%.2f",
            emoji,
            game_id,
            recommended_bet,
            bet_odds,
            bet_stake,
            profit_loss,
        )
        return profit_loss

    @staticmethod
    def _determine_outcome(
        recommended_bet: str,
        actual_winner: str,
        actual_total: float,
        total_line: Optional[float],
        pred,
    ) -> tuple[str, float]:
        """Returns (outcome, bet_odds) for the recommended bet."""
        if recommended_bet == "home_win":
            odds = pred["home_odds"] or 1.9
            outcome = "win" if actual_winner == "home_win" else "loss"
        elif recommended_bet == "away_win":
            odds = pred["away_odds"] or 1.9
            outcome = "win" if actual_winner == "away_win" else "loss"
        elif recommended_bet in ("over", "under"):
            if total_line is None:
                logger.warning(
                    "Cannot evaluate %s bet – no bookmaker total line stored", recommended_bet
                )
                odds = 1.0
                outcome = "void"
            elif recommended_bet == "over":
                odds = pred["total_over_odds"] or 1.9
                if actual_total > total_line:
                    outcome = "win"
                elif actual_total == total_line:
                    outcome = "push"
                else:
                    outcome = "loss"
            else:  # under
                odds = pred["total_under_odds"] or 1.9
                if actual_total < total_line:
                    outcome = "win"
                elif actual_total == total_line:
                    outcome = "push"
                else:
                    outcome = "loss"
        else:
            logger.warning("Unknown bet type '%s'", recommended_bet)
            odds = 1.0
            outcome = "void"

        return outcome, odds

    @staticmethod
    def _calculate_pnl(outcome: str, odds: float, stake: float) -> float:
        if outcome == "win":
            return round((odds - 1) * stake, 2)
        elif outcome == "loss":
            return round(-stake, 2)
        return 0.0

    @staticmethod
    def _log_summary(summary: dict, evaluated_today: int, profit_today: float) -> None:
        logger.info("-" * 60)
        logger.info("DAILY EVALUATION COMPLETE")
        logger.info("  Bets evaluated today : %d", evaluated_today)
        logger.info("  P&L today            : %.2f units", profit_today)
        logger.info("-" * 60)
        logger.info("CUMULATIVE PERFORMANCE")
        logger.info("  Total bets           : %d", summary.get("total_bets", 0))
        logger.info(
            "  Record               : %dW – %dL",
            summary.get("wins", 0),
            summary.get("losses", 0),
        )
        logger.info("  Total profit         : %.2f units", summary.get("total_profit", 0))
        logger.info("  Total staked         : %.2f units", summary.get("total_staked", 0))
        logger.info("  ROI                  : %.2f%%", summary.get("roi_pct", 0))
        logger.info("  Avg P&L per bet      : %.2f units", summary.get("avg_profit_per_bet", 0))
        by_type = summary.get("by_bet_type", {})
        if by_type:
            logger.info("  Breakdown by bet type:")
            for bt, stats in sorted(by_type.items()):
                logger.info(
                    "    %-10s %dW-%dL | profit=%.2f | ROI=%.1f%%",
                    bt,
                    stats["wins"],
                    stats["losses"],
                    stats["profit"],
                    stats["roi_pct"],
                )
        logger.info("-" * 60)

    @staticmethod
    def _log_calibration() -> None:
        data = get_calibration_data()
        if not data:
            return
        buckets = [(0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.70), (0.70, 1.01)]
        logger.info("CALIBRATION (predicted vs actual win rate)")
        any_bucket = False
        for lo, hi in buckets:
            group = [d for d in data if d["our_prob"] is not None and lo <= d["our_prob"] < hi]
            if not group:
                continue
            any_bucket = True
            wins = sum(1 for d in group if d["outcome"] == "win")
            label = f"{lo*100:.0f}-{min(hi*100, 100):.0f}%"
            logger.info(
                "  %s  predicted ~%.0f%%  actual %.0f%%  (n=%d)",
                label,
                (lo + hi) / 2 * 100,
                wins / len(group) * 100,
                len(group),
            )
        if not any_bucket:
            logger.info("  Not enough data yet for calibration")
        logger.info("-" * 60)
