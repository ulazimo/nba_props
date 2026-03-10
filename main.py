"""
Main Orchestrator
-----------------
Koordinira sve agente. Može se pokrenuti direktno ili kroz scheduler.
"""

import sys
import os

# Ensure project root is on path when running directly
sys.path.insert(0, os.path.dirname(__file__))

from datetime import date
from typing import Optional

from config.logging_config import setup_logger
from config.settings import CURRENT_SEASON
from data.database import initialize_db, get_games_for_date, save_prediction, get_games_pending_results

from agents.agent_scout import AgentScout
from agents.agent_matchup_expert import AgentMatchupExpert
from agents.agent_mathematician import AgentMathematician
from agents.agent_odds_specialist import AgentOddsSpecialist
from agents.agent_evaluator import AgentEvaluator

logger = setup_logger("Orchestrator")


class NBAOrchestrator:

    def __init__(self):
        logger.info("Initializing NBA Prediction System")
        initialize_db()
        self.scout = AgentScout()
        self.matchup_expert = AgentMatchupExpert()
        self.mathematician = AgentMathematician()
        self.odds_specialist = AgentOddsSpecialist()
        self.evaluator = AgentEvaluator()

    # ── Phase 1: 10:00 – Data Fetching ───────────────────────────────────

    def run_data_fetch(self, target_date: Optional[str] = None) -> None:
        logger.info("━━━ PHASE 1: DATA FETCH ━━━")
        try:
            self.scout.fetch_and_store_team_stats()
            # Invalidate league cache so next prediction run uses fresh stats
            self.matchup_expert.invalidate_cache()
        except Exception as exc:
            logger.error("Team stats fetch failed (non-fatal): %s", exc)

        try:
            self.scout.fetch_todays_games(target_date)
        except Exception as exc:
            logger.error("Games fetch failed: %s", exc)

        logger.info("Phase 1 complete")

    # ── Phase 2: 22:00 – Prediction Generation ───────────────────────────

    def run_prediction(self, target_date: Optional[str] = None) -> list[dict]:
        logger.info("━━━ PHASE 2: PREDICTION GENERATION ━━━")
        game_date = target_date or date.today().strftime("%Y-%m-%d")

        games = get_games_for_date(game_date)
        if not games:
            logger.warning("No games found for %s – nothing to predict", game_date)
            return []

        logger.info("Generating predictions for %d game(s) on %s", len(games), game_date)

        # Fetch fresh odds once for all games
        all_odds = []
        try:
            all_odds = self.odds_specialist.fetch_odds()
        except Exception as exc:
            logger.error("Odds fetch failed (predictions will proceed without odds): %s", exc)

        predictions: list[dict] = []

        for game in games:
            game_id = game["game_id"]
            home_team = game["home_team"]
            away_team = game["away_team"]

            logger.info("Processing: %s vs %s (game_id=%s)", home_team, away_team, game_id)

            try:
                # Step 1: Matchup analysis
                report = self.matchup_expert.analyze(home_team, away_team, game_date)
                if report is None:
                    logger.warning(
                        "Skipping %s – matchup analysis unavailable", game_id
                    )
                    continue

                # Step 2: Match odds to game
                odds_data = self.odds_specialist.match_odds_to_game(
                    home_team, away_team, all_odds
                )
                total_line = odds_data.total_line if odds_data else None

                # Step 3: Probability calculation
                prob_result = self.mathematician.calculate(report, total_line)
                if prob_result is None:
                    logger.warning(
                        "Skipping %s – probability calculation failed", game_id
                    )
                    continue

                # Step 4: Value bet analysis
                recommendations = self.odds_specialist.analyze_value(
                    prob_result, odds_data
                )

                # Step 5: Build prediction record
                best_bet = recommendations[0] if recommendations else None
                # bookmaker_total_line is the raw line from odds (may be None if no odds)
                # predicted_total is always the model's expected total
                # total_line in DB stores the bookmaker line used for O/U evaluation
                bookmaker_total_line = odds_data.total_line if odds_data else None
                pred_record = {
                    "game_id": game_id,
                    "home_team": home_team,
                    "away_team": away_team,
                    "home_b2b": report.breakdown.get("home_b2b", False),
                    "away_b2b": report.breakdown.get("away_b2b", False),
                    "home_win_prob": prob_result.home_win_prob,
                    "away_win_prob": prob_result.away_win_prob,
                    "predicted_total": prob_result.predicted_total,
                    "total_line": bookmaker_total_line,
                    "home_odds": odds_data.home_odds if odds_data else None,
                    "away_odds": odds_data.away_odds if odds_data else None,
                    "total_over_odds": odds_data.over_odds if odds_data else None,
                    "total_under_odds": odds_data.under_odds if odds_data else None,
                    "edge_home": None,
                    "edge_away": None,
                    "edge_over": None,
                    "edge_under": None,
                    "recommended_bet": best_bet.bet_type if best_bet else None,
                    "bet_stake": best_bet.kelly_stake if best_bet else None,
                    "raw_data": {
                        "matchup": report.to_dict(),
                        "probabilities": prob_result.to_dict(),
                        "recommendations": [r.to_dict() for r in recommendations],
                    },
                }

                # Fill edge values from recommendations
                for rec in recommendations:
                    if rec.bet_type == "home_win":
                        pred_record["edge_home"] = rec.edge
                    elif rec.bet_type == "away_win":
                        pred_record["edge_away"] = rec.edge
                    elif rec.bet_type == "over":
                        pred_record["edge_over"] = rec.edge
                    elif rec.bet_type == "under":
                        pred_record["edge_under"] = rec.edge

                pred_id = save_prediction(pred_record)
                pred_record["prediction_id"] = pred_id
                predictions.append(pred_record)

                self._log_prediction_summary(pred_record, home_team, away_team)

            except Exception as exc:
                logger.error(
                    "Unhandled error processing game %s (%s vs %s): %s",
                    game_id, home_team, away_team, exc,
                    exc_info=True,
                )

        logger.info("Phase 2 complete – %d prediction(s) generated", len(predictions))
        self._print_prediction_report(predictions, game_date)
        return predictions

    # ── Phase 3: 09:00 – Evaluation ──────────────────────────────────────

    def run_evaluation(self) -> dict:
        logger.info("━━━ PHASE 3: DAILY EVALUATION ━━━")

        # Step 0: Fetch results for games that have predictions but no score
        try:
            pending_ids = get_games_pending_results()
            if pending_ids:
                logger.info("Fetching results for %d pending game(s)", len(pending_ids))
                updated = self.scout.fetch_game_results(pending_ids)
                logger.info("Updated %d game result(s)", updated)
            else:
                logger.info("No pending game results to fetch")
        except Exception as exc:
            logger.error("Result fetching failed (non-fatal): %s", exc)

        try:
            summary = self.evaluator.run_daily_evaluation()
        except Exception as exc:
            logger.error("Evaluation failed: %s", exc, exc_info=True)
            summary = {}
        logger.info("Phase 3 complete")
        return summary

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _print_prediction_report(predictions: list[dict], game_date: str) -> None:
        W = 66
        THICK = "━" * W
        THIN  = "─" * W

        _ODDS_KEY = {
            "home_win": "home_odds",
            "away_win": "away_odds",
            "over":     "total_over_odds",
            "under":    "total_under_odds",
        }
        _EDGE_KEY = {
            "home_win": "edge_home",
            "away_win": "edge_away",
            "over":     "edge_over",
            "under":    "edge_under",
        }

        def bet_label(p: dict) -> str:
            bet = p["recommended_bet"]
            if bet == "home_win":
                return f"HOME WIN  ·  {p['home_team']}"
            if bet == "away_win":
                return f"AWAY WIN  ·  {p['away_team']}"
            line = p.get("total_line") or p.get("predicted_total") or "?"
            return f"{'OVER' if bet == 'over' else 'UNDER'}  {line:.1f}"

        bets    = [p for p in predictions if p.get("recommended_bet")]
        no_bets = [p for p in predictions if not p.get("recommended_bet")]

        print()
        print(THICK)
        print(f"  NBA PREDICTIONS  ·  {game_date}  ·  {len(predictions)} games")
        print(THICK)

        if bets:
            print(f"  ★  VALUE BETS  ({len(bets)})")
            print(THIN)
            for p in bets:
                bet  = p["recommended_bet"]
                odds = p.get(_ODDS_KEY.get(bet, "home_odds")) or 0.0
                edge = (p.get(_EDGE_KEY.get(bet, "edge_home")) or 0) * 100
                stake = p.get("bet_stake") or 0

                total_str = ""
                if p.get("predicted_total"):
                    total_str = f"  ·  total {p['predicted_total']:.1f}"
                    if p.get("total_line"):
                        total_str += f"  (line {p['total_line']:.1f})"

                home_label = p["home_team"] + (" (B2B)" if p.get("home_b2b") else "")
                away_label = p["away_team"] + (" (B2B)" if p.get("away_b2b") else "")
                print(f"  {home_label}  vs  {away_label}")
                print(
                    f"  P(home) {p['home_win_prob']*100:.1f}%  ·  "
                    f"P(away) {p['away_win_prob']*100:.1f}%"
                    f"{total_str}"
                )
                print(
                    f"  ► {bet_label(p)}"
                    f"  @  {odds:.2f}"
                    f"  ·  edge {edge:.1f}%"
                    f"  ·  stake {stake:.1f}%"
                )
                print(THIN)
        else:
            print("  Nema value opklada danas.")
            print(THIN)

        if no_bets:
            print(f"  ○  BEZ VREDNOSTI  ({len(no_bets)})")
            print(THIN)
            for p in no_bets:
                total_str = ""
                if p.get("predicted_total"):
                    total_str = f"  ·  total {p['predicted_total']:.1f}"
                    if p.get("total_line"):
                        total_str += f"  (line {p['total_line']:.1f})"
                home_label = p["home_team"] + (" (B2B)" if p.get("home_b2b") else "")
                away_label = p["away_team"] + (" (B2B)" if p.get("away_b2b") else "")
                print(
                    f"  {home_label}  vs  {away_label}"
                    f"  ·  P(home) {p['home_win_prob']*100:.1f}%"
                    f"  ·  P(away) {p['away_win_prob']*100:.1f}%"
                    f"{total_str}"
                )
            print(THIN)

        if bets:
            total_stake = sum(p.get("bet_stake") or 0 for p in bets)
            print(f"  {len(bets)} opklada  ·  ukupno izloženo: {total_stake:.1f}% bankrolla")
        print(THICK)
        print()

    @staticmethod
    def _log_prediction_summary(pred: dict, home: str, away: str) -> None:
        rec_bet = pred.get("recommended_bet")
        if rec_bet:
            _odds_key = {
                "home_win": "home_odds", "away_win": "away_odds",
                "over": "total_over_odds", "under": "total_under_odds",
            }
            odds_val = pred.get(_odds_key.get(rec_bet, "home_odds")) or 0.0
            line_str = ""
            if rec_bet in ("over", "under") and pred.get("total_line"):
                line_str = f" {pred['total_line']:.1f}"
            logger.info(
                "  ★ RECOMMENDATION: %s vs %s → BET: %s%s @ %.2f | stake: %.1f%%",
                home, away, rec_bet, line_str, odds_val, pred.get("bet_stake") or 0,
            )
        else:
            logger.info(
                "  ○ No value bet for %s vs %s "
                "(home_win=%.1f%% away_win=%.1f%%)",
                home, away,
                pred["home_win_prob"] * 100,
                pred["away_win_prob"] * 100,
            )


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="NBA Prediction System")
    parser.add_argument(
        "--phase",
        choices=["fetch", "predict", "evaluate", "all"],
        default="all",
        help="Which phase to run",
    )
    parser.add_argument("--date", default=None, help="Target date YYYY-MM-DD")
    args = parser.parse_args()

    orchestrator = NBAOrchestrator()

    if args.phase in ("fetch", "all"):
        orchestrator.run_data_fetch(args.date)
    if args.phase in ("predict", "all"):
        orchestrator.run_prediction(args.date)
    if args.phase in ("evaluate", "all"):
        orchestrator.run_evaluation()
