"""
Agent Odds Specialist (v2)
--------------------------
Povlači kvote sa The Odds API, upoređuje ih sa verovatnoćama modela
i identifikuje opkladne vrednosti (positive expected value).

Poboljšanja v2:
  - Devig korekcija implied probability (uklanja bookmaker margin)
  - Bolja team name normalizacija (više varijanti)
  - Total line se prati odvojeno od best-odds bookmaker-a
  - Sve value preporuke se loguju (ne samo prva)
"""

import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config.logging_config import setup_logger
from config.settings import (
    ODDS_API_KEY,
    ODDS_API_BASE_URL,
    ODDS_SPORT,
    ODDS_REGIONS,
    ODDS_MARKETS,
    ODDS_ODDS_FORMAT,
    MIN_EDGE,
    MIN_PROBABILITY,
    KELLY_FRACTION,
    MAX_STAKE,
    MIN_STAKE,
)
from agents.agent_mathematician import ProbabilityResult

logger = setup_logger("AgentOddsSpecialist")


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


def _normalize(name: str) -> str:
    """Normalize team name for matching."""
    n = name.lower().strip()
    n = n.replace("los angeles", "la")
    n = n.replace("golden st.", "golden state")
    n = n.replace("okc", "oklahoma city")
    n = n.replace("ny ", "new york ")
    n = n.replace("trail blazers", "trailblazers")
    return n


def _team_match(our_name: str, odds_name: str) -> bool:
    a, b = _normalize(our_name), _normalize(odds_name)
    if a == b:
        return True
    # Match on team nickname (last word) — unique across all 30 NBA teams.
    # Avoids false positives like "LA Clippers" vs "LA Lakers" sharing "la".
    a_nick = a.split()[-1] if a else ""
    b_nick = b.split()[-1] if b else ""
    return bool(a_nick and a_nick == b_nick)


class OddsData:
    def __init__(
        self,
        game_id: str,
        home_team: str,
        away_team: str,
        home_odds: float,
        away_odds: float,
        total_line: Optional[float],
        over_odds: Optional[float],
        under_odds: Optional[float],
        bookmaker: str,
        devig_home_odds: Optional[float] = None,
        devig_away_odds: Optional[float] = None,
    ):
        self.game_id = game_id
        self.home_team = home_team
        self.away_team = away_team
        self.home_odds = home_odds
        self.away_odds = away_odds
        self.total_line = total_line
        self.over_odds = over_odds
        self.under_odds = under_odds
        self.bookmaker = bookmaker
        # Co-occurring pair from the same bookmaker, used for a self-consistent overround
        self.devig_home_odds = devig_home_odds
        self.devig_away_odds = devig_away_odds


class BetRecommendation:
    def __init__(
        self,
        bet_type: str,
        odds: float,
        our_prob: float,
        implied_prob: float,
        devig_prob: float,
        edge: float,
        kelly_stake: float,
    ):
        self.bet_type = bet_type
        self.odds = odds
        self.our_prob = our_prob
        self.implied_prob = implied_prob
        self.devig_prob = devig_prob   # implied prob after removing bookmaker margin
        self.edge = edge
        self.kelly_stake = kelly_stake

    def to_dict(self) -> dict:
        return {
            "bet_type": self.bet_type,
            "odds": self.odds,
            "our_prob": self.our_prob,
            "implied_prob": self.implied_prob,
            "devig_prob": self.devig_prob,
            "edge": self.edge,
            "kelly_stake_pct": self.kelly_stake,
        }


class AgentOddsSpecialist:

    def __init__(self):
        if not ODDS_API_KEY:
            logger.warning(
                "ODDS_API_KEY not set – odds fetching will be skipped. "
                "Set ODDS_API_KEY in your .env file."
            )
        self._session = _build_session()

    # ── Public API ────────────────────────────────────────────────────────

    def fetch_odds(self) -> list[OddsData]:
        """Fetches current NBA odds from The Odds API."""
        if not ODDS_API_KEY:
            logger.warning("Skipping odds fetch – no API key configured")
            return []

        url = f"{ODDS_API_BASE_URL}/sports/{ODDS_SPORT}/odds"
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": ODDS_REGIONS,
            "markets": ODDS_MARKETS,
            "oddsFormat": ODDS_ODDS_FORMAT,
        }

        try:
            logger.info("Fetching odds from The Odds API")
            resp = self._session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            remaining = resp.headers.get("x-requests-remaining", "?")
            logger.info(
                "Odds API responded with %d events | requests remaining: %s",
                len(data),
                remaining,
            )
            return self._parse_odds(data)
        except requests.exceptions.HTTPError as exc:
            logger.error("Odds API HTTP error: %s", exc)
        except requests.exceptions.ConnectionError as exc:
            logger.error("Odds API connection error: %s", exc)
        except requests.exceptions.Timeout:
            logger.error("Odds API request timed out")
        except Exception as exc:
            logger.error("Unexpected error fetching odds: %s", exc)

        return []

    def analyze_value(
        self,
        prob_result: ProbabilityResult,
        odds_data: Optional[OddsData],
    ) -> list[BetRecommendation]:
        """
        Compares model probabilities with bookmaker odds.
        Uses devig (Shin method approximation) to remove bookmaker margin
        before computing edge, giving a fairer comparison.
        Returns all value bets sorted by edge descending.
        """
        if odds_data is None:
            logger.warning("No odds data provided – skipping value analysis")
            return []

        recommendations: list[BetRecommendation] = []

        # Moneyline implied probs from best available odds (used for raw_implied in recs)
        h2h_implied_home = 1.0 / odds_data.home_odds if odds_data.home_odds > 1.0 else 0.0
        h2h_implied_away = 1.0 / odds_data.away_odds if odds_data.away_odds > 1.0 else 0.0
        # Overround from a self-consistent same-bookmaker pair to avoid synthetic margin
        if odds_data.devig_home_odds and odds_data.devig_away_odds:
            h2h_overround = (1.0 / odds_data.devig_home_odds) + (1.0 / odds_data.devig_away_odds)
        else:
            h2h_overround = h2h_implied_home + h2h_implied_away

        # O/U devig
        ou_implied_over = 1.0 / odds_data.over_odds if (odds_data.over_odds and odds_data.over_odds > 1.0) else 0.0
        ou_implied_under = 1.0 / odds_data.under_odds if (odds_data.under_odds and odds_data.under_odds > 1.0) else 0.0
        ou_overround = ou_implied_over + ou_implied_under

        total_line = odds_data.total_line
        candidates = [
            ("home_win", prob_result.home_win_prob, odds_data.home_odds, h2h_implied_home, h2h_overround, None),
            ("away_win", prob_result.away_win_prob, odds_data.away_odds, h2h_implied_away, h2h_overround, None),
        ]
        if odds_data.over_odds and ou_overround > 0:
            candidates.append(("over", prob_result.over_prob, odds_data.over_odds, ou_implied_over, ou_overround, total_line))
        if odds_data.under_odds and ou_overround > 0:
            candidates.append(("under", prob_result.under_prob, odds_data.under_odds, ou_implied_under, ou_overround, total_line))

        for bet_type, our_prob, odds, raw_implied, overround, line in candidates:
            if odds <= 1.0 or raw_implied <= 0:
                continue

            # Devig: remove bookmaker margin proportionally (Shin approximation)
            devig_prob = raw_implied / overround if overround > 0 else raw_implied
            edge = our_prob - devig_prob

            logger.debug(
                "  %s: our=%.2f%% implied=%.2f%% devig=%.2f%% edge=%.2f%%",
                bet_type,
                our_prob * 100,
                raw_implied * 100,
                devig_prob * 100,
                edge * 100,
            )

            if edge >= MIN_EDGE and our_prob >= MIN_PROBABILITY:
                kelly = self._kelly_stake(our_prob, odds)
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
                line_str = f" {line:.1f}" if line is not None else ""
                logger.info(
                    "  VALUE BET → %s%s @ %.2f | our=%.1f%% devig=%.1f%% edge=%.1f%% | kelly=%.1f%%",
                    bet_type, line_str, odds,
                    our_prob * 100, devig_prob * 100, edge * 100, kelly,
                )

        recommendations.sort(key=lambda r: r.edge, reverse=True)

        if not recommendations:
            logger.info(
                "  No value bets (home_win edge=%.1f%% away_win edge=%.1f%%)",
                (prob_result.home_win_prob - (h2h_implied_home / h2h_overround if h2h_overround else 0)) * 100,
                (prob_result.away_win_prob - (h2h_implied_away / h2h_overround if h2h_overround else 0)) * 100,
            )

        return recommendations

    def match_odds_to_game(
        self, home_team: str, away_team: str, all_odds: list[OddsData]
    ) -> Optional[OddsData]:
        """Finds the best matching OddsData entry for a given game."""
        for od in all_odds:
            if _team_match(home_team, od.home_team) and _team_match(away_team, od.away_team):
                return od
            if _team_match(home_team, od.away_team) and _team_match(away_team, od.home_team):
                return OddsData(
                    game_id=od.game_id,
                    home_team=od.away_team,
                    away_team=od.home_team,
                    home_odds=od.away_odds,
                    away_odds=od.home_odds,
                    total_line=od.total_line,
                    over_odds=od.over_odds,
                    under_odds=od.under_odds,
                    bookmaker=od.bookmaker,
                    devig_home_odds=od.devig_away_odds,
                    devig_away_odds=od.devig_home_odds,
                )
        logger.warning("No odds match found for %s vs %s", home_team, away_team)
        return None

    # ── Private helpers ───────────────────────────────────────────────────

    @staticmethod
    def _kelly_stake(prob: float, odds: float) -> float:
        """Fractional Kelly Criterion → percentage of bankroll."""
        b = odds - 1.0
        q = 1.0 - prob
        kelly = (b * prob - q) / b
        kelly = max(0.0, kelly) * KELLY_FRACTION * 100
        if kelly <= 0:
            return 0.0
        return round(max(MIN_STAKE, min(kelly, MAX_STAKE)), 2)

    @staticmethod
    def _parse_odds(data: list[dict]) -> list[OddsData]:
        results: list[OddsData] = []
        for event in data:
            try:
                home_team = event.get("home_team", "")
                away_team = event.get("away_team", "")
                game_id = event.get("id", "")

                best_home = best_away = None
                best_over = best_under = None
                devig_home = devig_away = None
                total_line_for_over = None
                total_line_for_under = None
                bookmaker_name = ""

                for bookmaker in event.get("bookmakers", []):
                    bk_name = bookmaker.get("title", "")
                    for market in bookmaker.get("markets", []):
                        key = market.get("key")
                        outcomes = market.get("outcomes", [])

                        if key == "h2h":
                            bk_home = bk_away = None
                            for outcome in outcomes:
                                price = float(outcome.get("price", 0))
                                if outcome["name"] == home_team:
                                    bk_home = price
                                elif outcome["name"] == away_team:
                                    bk_away = price
                            if bk_home:
                                if best_home is None or bk_home > best_home:
                                    best_home = bk_home
                                    bookmaker_name = bk_name
                            if bk_away:
                                if best_away is None or bk_away > best_away:
                                    best_away = bk_away
                            # Consistent pair for devig: same-book pair with best home odds
                            if bk_home and bk_away and (devig_home is None or bk_home > devig_home):
                                devig_home = bk_home
                                devig_away = bk_away

                        elif key == "totals":
                            for outcome in outcomes:
                                price = float(outcome.get("price", 0))
                                point = float(outcome.get("point", 0))
                                if outcome["name"] == "Over":
                                    if best_over is None or price > best_over:
                                        best_over = price
                                        total_line_for_over = point
                                elif outcome["name"] == "Under":
                                    if best_under is None or price > best_under:
                                        best_under = price
                                        total_line_for_under = point

                if best_home and best_away:
                    # Use over line as canonical; fall back to under line if over missing
                    total_line = total_line_for_over or total_line_for_under
                    results.append(
                        OddsData(
                            game_id=game_id,
                            home_team=home_team,
                            away_team=away_team,
                            home_odds=best_home,
                            away_odds=best_away,
                            total_line=total_line,
                            over_odds=best_over,
                            under_odds=best_under,
                            bookmaker=bookmaker_name,
                            devig_home_odds=devig_home,
                            devig_away_odds=devig_away,
                        )
                    )
            except Exception as exc:
                logger.error("Error parsing odds event: %s | data=%s", exc, event)

        logger.info("Parsed %d odds entries", len(results))
        return results
