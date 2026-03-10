"""
Props Orchestrator
------------------
Coordinates the NBA player props prediction pipeline.

  --phase fetch     : fetch player stats via nba_api
  --phase predict   : generate player props predictions for today
  --phase evaluate  : evaluate yesterday's props against actual boxscores
  --phase all       : fetch + predict + evaluate
"""

import json
import sys
import os

# Ensure project root is on path when running directly
sys.path.insert(0, os.path.dirname(__file__))

from datetime import date
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config.logging_config import setup_logger
from config.settings import PROPS_SEASON
from data.database import (
    initialize_db,
    get_games_with_event_ids,
    get_player_stats_by_name,
    save_player_props_prediction,
    get_player_props_for_game,
    get_finished_games_without_props_evaluation,
    save_player_props_result,
    get_props_profitability_summary,
)

from agents.agent_props_scout import AgentPropsScout
from agents.agent_props_matchup import AgentPropsMatchup
from agents.agent_props_mathematician import AgentPropsMathematician
from agents.agent_props_odds import AgentPropsOdds, get_last_requests_remaining

logger = setup_logger("PropsOrchestrator")

CDN_BOXSCORE = "https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{game_id}.json"
CDN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
}


def _build_cdn_session() -> requests.Session:
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


class PropsOrchestrator:

    def __init__(self):
        logger.info("Initializing NBA Props Prediction System")
        initialize_db()
        self.scout = AgentPropsScout()
        self.matchup = AgentPropsMatchup()
        self.mathematician = AgentPropsMathematician()
        self.props_odds = AgentPropsOdds()
        self._cdn_session = _build_cdn_session()

    # ── Phase 1: Data Fetching ────────────────────────────────────────────

    def run_props_fetch(self) -> None:
        logger.info("━━━ PROPS PHASE 1: DATA FETCH ━━━")
        try:
            ok = self.scout.fetch_and_store_season_stats()
            if ok:
                logger.info("Season stats fetch completed successfully")
            else:
                logger.warning("Season stats fetch returned no data")
        except Exception as exc:
            logger.error("Season stats fetch failed (non-fatal): %s", exc)

        try:
            ok = self.scout.fetch_and_store_recent_logs()
            if ok:
                logger.info("Recent game logs fetch completed successfully")
            else:
                logger.warning("Recent game logs fetch returned no data")
        except Exception as exc:
            logger.error("Recent game logs fetch failed (non-fatal): %s", exc)

        logger.info("Props Phase 1 complete")

    # ── Phase 2: Prediction Generation ───────────────────────────────────

    def run_props_predict(self, target_date: Optional[str] = None) -> list[dict]:
        logger.info("━━━ PROPS PHASE 2: PREDICTION GENERATION ━━━")
        game_date = target_date or date.today().strftime("%Y-%m-%d")

        games = get_games_with_event_ids(game_date)
        if not games:
            logger.warning(
                "No games found for %s – nothing to predict", game_date
            )
            return []

        games_with_events = [g for g in games if g["odds_event_id"]]
        if not games_with_events:
            logger.warning(
                "No games with odds_event_id found for %s – "
                "run main.py --phase predict first to populate event IDs",
                game_date,
            )

        logger.info(
            "Generating props predictions for %d game(s) with event IDs on %s",
            len(games_with_events),
            game_date,
        )

        all_predictions: list[dict] = []

        for game in games_with_events:
            game_id = game["game_id"]
            home_team = game["home_team"]
            away_team = game["away_team"]
            event_id = game["odds_event_id"]

            logger.info(
                "Processing props: %s vs %s (game_id=%s event=%s)",
                home_team,
                away_team,
                game_id,
                event_id,
            )

            try:
                lines = self.props_odds.fetch_props_for_event(event_id)
                if not lines:
                    logger.warning(
                        "No player props lines returned for event %s", event_id
                    )
                    continue

                logger.info(
                    "  Received %d player lines for %s vs %s",
                    len(lines),
                    home_team,
                    away_team,
                )

                for player_line in lines:
                    try:
                        ps = get_player_stats_by_name(
                            player_line.player_name, PROPS_SEASON
                        )
                        if ps is None:
                            # Try normalized name lookup (best-effort)
                            logger.debug(
                                "No player stats found for '%s' – skipping",
                                player_line.player_name,
                            )
                            continue

                        # Determine if player's team is home
                        player_team = ps["team_name"] or ""
                        is_home = player_team == home_team

                        report = self.matchup.analyze(
                            ps, home_team, away_team, is_home
                        )
                        if report is None:
                            logger.warning(
                                "Matchup analysis failed for %s – skipping",
                                player_line.player_name,
                            )
                            continue

                        prob = self.mathematician.calculate(report, player_line.line)
                        if prob is None:
                            logger.warning(
                                "Probability calculation failed for %s – skipping",
                                player_line.player_name,
                            )
                            continue

                        recs = self.props_odds.analyze_value_props(prob, player_line)
                        best = recs[0] if recs else None

                        # Build prediction record
                        edge_over = None
                        edge_under = None
                        for rec in recs:
                            if rec.bet_type == "over":
                                edge_over = rec.edge
                            elif rec.bet_type == "under":
                                edge_under = rec.edge

                        pred_record = {
                            "game_id": game_id,
                            "odds_event_id": event_id,
                            "player_id": ps["player_id"],
                            "player_name": player_line.player_name,
                            "team_name": report.team_name,
                            "opponent_team": report.opponent_team,
                            "projected_points": prob.projected_points,
                            "points_line": player_line.line,
                            "over_prob": prob.over_prob,
                            "under_prob": prob.under_prob,
                            "over_odds": player_line.over_odds,
                            "under_odds": player_line.under_odds,
                            "edge_over": edge_over,
                            "edge_under": edge_under,
                            "recommended_bet": best.bet_type if best else None,
                            "bet_stake": best.kelly_stake if best else None,
                            "raw_data": {
                                "matchup": report.to_dict(),
                                "probabilities": prob.to_dict(),
                                "recommendations": [r.to_dict() for r in recs],
                            },
                        }

                        pred_id = save_player_props_prediction(pred_record)
                        pred_record["prediction_id"] = pred_id
                        all_predictions.append(pred_record)

                    except Exception as exc:
                        logger.error(
                            "Unhandled error for player '%s' in game %s: %s",
                            player_line.player_name,
                            game_id,
                            exc,
                            exc_info=True,
                        )

            except Exception as exc:
                logger.error(
                    "Unhandled error processing game %s (%s vs %s): %s",
                    game_id,
                    home_team,
                    away_team,
                    exc,
                    exc_info=True,
                )

        remaining = get_last_requests_remaining()
        if remaining is not None:
            logger.info("Props API requests remaining: %s", remaining)

        logger.info(
            "Props Phase 2 complete – %d prediction(s) generated",
            len(all_predictions),
        )
        self._print_props_report(all_predictions, game_date)
        return all_predictions

    # ── Phase 3: Evaluation ───────────────────────────────────────────────

    def run_props_evaluate(self) -> None:
        logger.info("━━━ PROPS PHASE 3: EVALUATION ━━━")

        finished_games = get_finished_games_without_props_evaluation()
        if not finished_games:
            logger.info("No finished games with unevaluated props predictions")
            return

        logger.info(
            "Evaluating props for %d finished game(s)", len(finished_games)
        )

        total_evaluated = 0
        for game in finished_games:
            game_id = game["game_id"]
            logger.info("Evaluating game %s", game_id)

            try:
                # Fetch boxscore to get actual player points
                player_scores = self._fetch_player_scores(game_id)
                if not player_scores:
                    logger.warning(
                        "Could not fetch player scores for game %s – skipping",
                        game_id,
                    )
                    continue

                predictions = get_player_props_for_game(game_id)
                if not predictions:
                    logger.info("No props predictions for game %s", game_id)
                    continue

                evaluated = 0
                for pred in predictions:
                    try:
                        if pred["recommended_bet"] is None:
                            continue

                        # Look up actual points for this player
                        p_name = pred["player_name"]
                        actual_pts = self._lookup_player_points(
                            p_name, player_scores
                        )
                        if actual_pts is None:
                            logger.debug(
                                "No actual points found for %s in game %s",
                                p_name,
                                game_id,
                            )
                            continue

                        bet_type = pred["recommended_bet"]
                        line = pred["points_line"] or 0.0

                        if bet_type == "over":
                            bet_odds = pred["over_odds"]
                            outcome = "win" if actual_pts > line else "loss"
                        else:  # under
                            bet_odds = pred["under_odds"]
                            outcome = "win" if actual_pts <= line else "loss"

                        stake = pred["bet_stake"] or 0.0
                        if outcome == "win":
                            profit_loss = round(stake * (bet_odds - 1.0), 4)
                        else:
                            profit_loss = -stake

                        result = {
                            "prediction_id": pred["id"],
                            "game_id": game_id,
                            "player_id": pred["player_id"],
                            "actual_points": actual_pts,
                            "bet_type": bet_type,
                            "bet_odds": bet_odds,
                            "stake": stake,
                            "outcome": outcome,
                            "profit_loss": profit_loss,
                        }
                        save_player_props_result(result)
                        evaluated += 1

                        logger.info(
                            "  %s: actual=%d line=%.1f bet=%s → %s (P&L %.2f%%)",
                            p_name,
                            actual_pts,
                            line,
                            bet_type,
                            outcome,
                            profit_loss,
                        )

                    except Exception as exc:
                        logger.error(
                            "Error evaluating prediction %s: %s",
                            pred["id"],
                            exc,
                        )

                total_evaluated += evaluated
                logger.info(
                    "  Evaluated %d props bets for game %s", evaluated, game_id
                )

            except Exception as exc:
                logger.error(
                    "Error evaluating game %s: %s", game_id, exc, exc_info=True
                )

        logger.info("Props Phase 3 complete – %d result(s) recorded", total_evaluated)

        # Log calibration summary if enough data
        try:
            summary = get_props_profitability_summary()
            if summary and summary.get("total_bets", 0) >= 10:
                logger.info(
                    "Props P&L summary: %d bets | %dW-%dL | "
                    "profit %.2f%% | ROI %.2f%%",
                    summary["total_bets"],
                    summary["wins"],
                    summary["losses"],
                    summary["total_profit"],
                    summary["roi_pct"],
                )
        except Exception as exc:
            logger.error("Error fetching props profitability summary: %s", exc)

    # ── Helpers ───────────────────────────────────────────────────────────

    def _fetch_player_scores(self, game_id: str) -> Optional[dict[str, int]]:
        """
        Fetches the CDN boxscore and returns a mapping of
        player_name → actual_points for the given game.
        Returns None if the boxscore cannot be fetched.
        """
        url = CDN_BOXSCORE.format(game_id=game_id)
        try:
            resp = self._cdn_session.get(url, timeout=15)
            resp.raise_for_status()
            game_data = resp.json().get("game", {})

            scores: dict[str, int] = {}
            for side in ("homeTeam", "awayTeam"):
                team = game_data.get(side, {})
                for player in team.get("players", []):
                    try:
                        stats = player.get("statistics", {})
                        name = (
                            player.get("name", "")
                            or f"{player.get('firstName', '')} {player.get('familyName', '')}".strip()
                        )
                        pts = int(stats.get("points", 0) or 0)
                        if name:
                            scores[name] = pts
                    except (ValueError, TypeError):
                        pass

            logger.info(
                "Fetched boxscore for game %s: %d players", game_id, len(scores)
            )
            return scores if scores else None

        except requests.exceptions.HTTPError as exc:
            logger.error("CDN boxscore HTTP error (game=%s): %s", game_id, exc)
        except requests.exceptions.ConnectionError as exc:
            logger.error("CDN boxscore connection error (game=%s): %s", game_id, exc)
        except requests.exceptions.Timeout:
            logger.error("CDN boxscore request timed out (game=%s)", game_id)
        except Exception as exc:
            logger.error(
                "Unexpected error fetching boxscore (game=%s): %s", game_id, exc
            )
        return None

    @staticmethod
    def _lookup_player_points(
        player_name: str, scores: dict[str, int]
    ) -> Optional[int]:
        """
        Looks up actual points for a player by name.
        Tries exact match first, then last-name match.
        """
        if player_name in scores:
            return scores[player_name]

        # Last-name fallback
        last_name = player_name.strip().split()[-1] if player_name.strip() else ""
        if last_name:
            for name, pts in scores.items():
                if name.strip().split()[-1] == last_name:
                    return pts

        return None

    @staticmethod
    def _print_props_report(predictions: list[dict], game_date: str) -> None:
        W = 66
        THICK = "━" * W
        THIN = "─" * W

        bets = [p for p in predictions if p.get("recommended_bet")]
        no_bets = [p for p in predictions if not p.get("recommended_bet")]

        print()
        print(THICK)
        print(f"  NBA PLAYER PROPS  ·  {game_date}  ·  {len(predictions)} players")
        print(THICK)

        if bets:
            print(f"  ★  VALUE BETS  ({len(bets)})")
            print(THIN)
            for p in bets:
                bet = p["recommended_bet"]
                if bet == "over":
                    odds = p.get("over_odds") or 0.0
                    edge = (p.get("edge_over") or 0) * 100
                else:
                    odds = p.get("under_odds") or 0.0
                    edge = (p.get("edge_under") or 0) * 100
                stake = p.get("bet_stake") or 0.0
                line = p.get("points_line") or 0.0
                proj = p.get("projected_points") or 0.0

                # Get sigma from raw_data
                sigma_str = ""
                try:
                    raw = p.get("raw_data", {})
                    if isinstance(raw, str):
                        raw = json.loads(raw)
                    sigma = raw.get("probabilities", {}).get("sigma")
                    if sigma is not None:
                        sigma_str = f"  ·  σ={sigma:.1f}"
                except Exception:
                    pass

                print(
                    f"  {p['player_name']} ({p['team_name']})"
                    f"  vs  {p['opponent_team']}"
                )
                print(
                    f"  Proj: {proj:.1f} pts{sigma_str}  ·  Line: {line:.1f}"
                )
                print(
                    f"  ► {bet.upper()} {line:.1f}"
                    f"  @  {odds:.2f}"
                    f"  ·  edge {edge:.1f}%"
                    f"  ·  stake {stake:.1f}%"
                )
                print(THIN)
        else:
            print("  No value props bets today.")
            print(THIN)

        if no_bets:
            print(f"  ○  NO VALUE  ({len(no_bets)})")
            print(THIN)
            for p in no_bets:
                line = p.get("points_line") or 0.0
                proj = p.get("projected_points") or 0.0
                over_p = (p.get("over_prob") or 0) * 100
                under_p = (p.get("under_prob") or 0) * 100
                print(
                    f"  {p['player_name']} ({p['team_name']})"
                    f"  vs  {p['opponent_team']}"
                    f"  ·  proj {proj:.1f}"
                    f"  ·  line {line:.1f}"
                    f"  ·  P(over) {over_p:.1f}%"
                    f"  ·  P(under) {under_p:.1f}%"
                )
            print(THIN)

        if bets:
            total_stake = sum(p.get("bet_stake") or 0.0 for p in bets)
            print(
                f"  {len(bets)} props bets  ·  "
                f"total exposure: {total_stake:.1f}% bankroll"
            )
        print(THICK)
        print()


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="NBA Props Prediction System")
    parser.add_argument(
        "--phase",
        choices=["fetch", "predict", "evaluate", "all"],
        default="all",
        help="Which phase to run",
    )
    parser.add_argument("--date", default=None, help="Target date YYYY-MM-DD")
    args = parser.parse_args()

    orchestrator = PropsOrchestrator()

    if args.phase in ("fetch", "all"):
        orchestrator.run_props_fetch()
    if args.phase in ("predict", "all"):
        orchestrator.run_props_predict(args.date)
    if args.phase in ("evaluate", "all"):
        orchestrator.run_props_evaluate()
