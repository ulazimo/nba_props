"""
Agent Props Odds
----------------
Fetches player points props from The Odds API (event-specific endpoint).
Deduces best over/under odds per player across bookmakers.
Uses consistent same-bookmaker pair for devig overround.
"""

import unicodedata
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config.logging_config import setup_logger
from config.settings import (
    ODDS_API_KEY,
    ODDS_API_BASE_URL,
    ODDS_SPORT,
    ODDS_ODDS_FORMAT,
    PROPS_ODDS_REGIONS,
    MIN_EDGE,
    MIN_PROBABILITY,
    KELLY_FRACTION,
    MAX_STAKE,
    MIN_STAKE,
)
from agents.agent_odds_specialist import BetRecommendation
from agents.agent_props_mathematician import PlayerProbResult

logger = setup_logger("AgentPropsOdds")

# Most-recent props request remaining header (updated on each fetch)
_last_requests_remaining: Optional[str] = None


def _normalize_player_name(name: str) -> str:
    """Remove accents, lowercase, strip whitespace for fuzzy matching."""
    nfkd = unicodedata.normalize("NFKD", name.lower().strip())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _kelly_stake(prob: float, odds: float) -> float:
    """Fractional Kelly Criterion → percentage of bankroll."""
    b = odds - 1.0
    q = 1.0 - prob
    kelly = (b * prob - q) / b
    kelly = max(0.0, kelly) * KELLY_FRACTION * 100
    if kelly <= 0:
        return 0.0
    return round(max(MIN_STAKE, min(kelly, MAX_STAKE)), 2)


class PlayerPropsLine:

    def __init__(
        self,
        player_name: str,
        normalized_name: str,
        line: float,
        over_odds: float,
        under_odds: float,
        bookmaker: str,
        devig_over_odds: Optional[float] = None,
        devig_under_odds: Optional[float] = None,
    ):
        self.player_name = player_name
        self.normalized_name = normalized_name
        self.line = line
        self.over_odds = over_odds
        self.under_odds = under_odds
        self.bookmaker = bookmaker
        self.devig_over_odds = devig_over_odds
        self.devig_under_odds = devig_under_odds


class AgentPropsOdds:

    def __init__(self):
        if not ODDS_API_KEY:
            logger.warning(
                "ODDS_API_KEY not set – props odds fetching will be skipped. "
                "Set ODDS_API_KEY in your .env file."
            )
        self._session = _build_session()

    def fetch_props_for_event(self, event_id: str) -> list[PlayerPropsLine]:
        """
        Fetches player_points market for a specific event from The Odds API.
        Returns one PlayerPropsLine per player (best odds across bookmakers,
        plus a consistent same-bookmaker devig pair).
        """
        global _last_requests_remaining

        if not ODDS_API_KEY:
            logger.warning("Skipping props fetch – no API key configured")
            return []

        url = f"{ODDS_API_BASE_URL}/sports/{ODDS_SPORT}/events/{event_id}/odds"
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": PROPS_ODDS_REGIONS,
            "markets": "player_points",
            "oddsFormat": ODDS_ODDS_FORMAT,
        }

        try:
            logger.info("Fetching player props for event %s", event_id)
            resp = self._session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            remaining = resp.headers.get("x-requests-remaining", "?")
            _last_requests_remaining = remaining
            logger.info(
                "Props API responded for event %s | requests remaining: %s",
                event_id,
                remaining,
            )
            return self._parse_player_props(data)
        except requests.exceptions.HTTPError as exc:
            logger.error("Props API HTTP error (event=%s): %s", event_id, exc)
        except requests.exceptions.ConnectionError as exc:
            logger.error("Props API connection error (event=%s): %s", event_id, exc)
        except requests.exceptions.Timeout:
            logger.error("Props API request timed out (event=%s)", event_id)
        except Exception as exc:
            logger.error(
                "Unexpected error fetching props (event=%s): %s", event_id, exc
            )

        return []

    def match_player(
        self, our_name: str, lines: list[PlayerPropsLine]
    ) -> Optional[PlayerPropsLine]:
        """
        Finds the matching PlayerPropsLine for a given player name.
        Tries exact normalized match first, then last-name match as fallback.
        """
        normalized_ours = _normalize_player_name(our_name)

        # Exact normalized match
        for line in lines:
            if line.normalized_name == normalized_ours:
                return line

        # Last-name fallback
        our_last = normalized_ours.split()[-1] if normalized_ours.split() else ""
        if our_last:
            for line in lines:
                line_last = (
                    line.normalized_name.split()[-1]
                    if line.normalized_name.split()
                    else ""
                )
                if line_last and line_last == our_last:
                    logger.debug(
                        "Player name matched by last name: '%s' → '%s'",
                        our_name,
                        line.player_name,
                    )
                    return line

        logger.debug("No props line found for player '%s'", our_name)
        return None

    def analyze_value_props(
        self,
        prob_result: PlayerProbResult,
        line_data: PlayerPropsLine,
    ) -> list[BetRecommendation]:
        """
        Compares model over/under probabilities with bookmaker odds.
        Uses devig (consistent same-bookmaker pair) to remove bookmaker margin.
        Returns all value bets sorted by edge descending.
        """
        recommendations: list[BetRecommendation] = []

        if line_data.over_odds <= 1.0 or line_data.under_odds <= 1.0:
            logger.warning(
                "Invalid odds for %s: over=%.2f under=%.2f",
                line_data.player_name,
                line_data.over_odds,
                line_data.under_odds,
            )
            return []

        # Raw implied probabilities from best available odds
        implied_over = 1.0 / line_data.over_odds
        implied_under = 1.0 / line_data.under_odds

        # Use consistent same-bookmaker pair for devig to avoid synthetic margin
        if line_data.devig_over_odds and line_data.devig_under_odds:
            devig_over_raw = 1.0 / line_data.devig_over_odds
            devig_under_raw = 1.0 / line_data.devig_under_odds
            overround = devig_over_raw + devig_under_raw
        else:
            overround = implied_over + implied_under

        if overround <= 0:
            logger.warning(
                "Overround=%.4f for %s – skipping devig",
                overround,
                line_data.player_name,
            )
            return []

        candidates = [
            ("over", prob_result.over_prob, line_data.over_odds, implied_over),
            ("under", prob_result.under_prob, line_data.under_odds, implied_under),
        ]

        for bet_type, our_prob, odds, raw_implied in candidates:
            if odds <= 1.0 or raw_implied <= 0:
                continue

            devig_prob = raw_implied / overround
            edge = our_prob - devig_prob

            logger.debug(
                "  props %s %s: our=%.2f%% implied=%.2f%% devig=%.2f%% edge=%.2f%%",
                line_data.player_name,
                bet_type,
                our_prob * 100,
                raw_implied * 100,
                devig_prob * 100,
                edge * 100,
            )

            if edge >= MIN_EDGE and our_prob >= MIN_PROBABILITY:
                kelly = _kelly_stake(our_prob, odds)
                rec = BetRecommendation(
                    bet_type=bet_type,
                    odds=odds,
                    our_prob=our_prob,
                    implied_prob=raw_implied,
                    devig_prob=round(devig_prob, 4),
                    edge=round(edge, 4),
                    kelly_stake=kelly,
                )
                recommendations.append(rec)
                logger.info(
                    "  PROPS VALUE BET → %s %s %.1f @ %.2f | "
                    "our=%.1f%% devig=%.1f%% edge=%.1f%% | kelly=%.1f%%",
                    line_data.player_name,
                    bet_type,
                    line_data.line,
                    odds,
                    our_prob * 100,
                    devig_prob * 100,
                    edge * 100,
                    kelly,
                )

        recommendations.sort(key=lambda r: r.edge, reverse=True)

        if not recommendations:
            logger.info(
                "  No props value (over edge=%.1f%% under edge=%.1f%%) for %s",
                (prob_result.over_prob - (implied_over / overround)) * 100,
                (prob_result.under_prob - (implied_under / overround)) * 100,
                line_data.player_name,
            )

        return recommendations

    # ── Private helpers ───────────────────────────────────────────────────

    @staticmethod
    def _parse_player_props(data: dict) -> list[PlayerPropsLine]:
        """
        Parse The Odds API event-specific response for player_points market.
        Groups outcomes by player name, collects Over/Under.
        Tracks best over/under odds across bookmakers AND a consistent
        same-bookmaker pair for devig.
        """
        results: list[PlayerPropsLine] = []

        # player_name → { line, best_over, best_under, bookmaker,
        #                  devig_over, devig_under }
        player_map: dict[str, dict] = {}

        for bookmaker in data.get("bookmakers", []):
            bk_name = bookmaker.get("title", "")
            for market in bookmaker.get("markets", []):
                if market.get("key") != "player_points":
                    continue

                # Collect all outcomes for this bookmaker for same-book devig pairs
                bk_player_outcomes: dict[str, dict] = {}

                for outcome in market.get("outcomes", []):
                    try:
                        p_name = outcome.get("description", "") or outcome.get(
                            "name", ""
                        )
                        bet_side = outcome.get("name", "")  # "Over" or "Under"
                        price = float(outcome.get("price", 0))
                        point = float(outcome.get("point", 0))

                        if not p_name or price <= 1.0:
                            continue

                        if p_name not in bk_player_outcomes:
                            bk_player_outcomes[p_name] = {
                                "over": None,
                                "under": None,
                                "line": point,
                            }

                        if bet_side == "Over":
                            bk_player_outcomes[p_name]["over"] = price
                            bk_player_outcomes[p_name]["line"] = point
                        elif bet_side == "Under":
                            bk_player_outcomes[p_name]["under"] = price

                    except (ValueError, TypeError) as exc:
                        logger.debug("Error parsing props outcome: %s", exc)

                # Merge into player_map, tracking bests and devig pair
                for p_name, sides in bk_player_outcomes.items():
                    bk_over = sides.get("over")
                    bk_under = sides.get("under")
                    bk_line = sides.get("line", 0.0)

                    if p_name not in player_map:
                        player_map[p_name] = {
                            "line": bk_line,
                            "best_over": None,
                            "best_under": None,
                            "bookmaker": bk_name,
                            "devig_over": None,
                            "devig_under": None,
                        }

                    pm = player_map[p_name]

                    if bk_over is not None:
                        if pm["best_over"] is None or bk_over > pm["best_over"]:
                            pm["best_over"] = bk_over
                            pm["bookmaker"] = bk_name
                            pm["line"] = bk_line

                    if bk_under is not None:
                        if pm["best_under"] is None or bk_under > pm["best_under"]:
                            pm["best_under"] = bk_under

                    # Same-book devig pair: prefer pair where over odds are highest
                    if (
                        bk_over is not None
                        and bk_under is not None
                        and (
                            pm["devig_over"] is None
                            or bk_over > pm["devig_over"]
                        )
                    ):
                        pm["devig_over"] = bk_over
                        pm["devig_under"] = bk_under

        # Convert player_map to PlayerPropsLine objects
        for p_name, pm in player_map.items():
            if pm["best_over"] is None or pm["best_under"] is None:
                continue
            results.append(
                PlayerPropsLine(
                    player_name=p_name,
                    normalized_name=_normalize_player_name(p_name),
                    line=pm["line"],
                    over_odds=pm["best_over"],
                    under_odds=pm["best_under"],
                    bookmaker=pm["bookmaker"],
                    devig_over_odds=pm["devig_over"],
                    devig_under_odds=pm["devig_under"],
                )
            )

        logger.info(
            "Parsed %d player props lines from event response", len(results)
        )
        return results


def get_last_requests_remaining() -> Optional[str]:
    """Returns the most recent x-requests-remaining header value from props fetches."""
    return _last_requests_remaining
