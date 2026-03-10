import os
from dotenv import load_dotenv

load_dotenv()

# ── The Odds API ──────────────────────────────────────────────────────────────
ODDS_API_KEY: str = os.getenv("ODDS_API_KEY", "")
ODDS_API_BASE_URL: str = "https://api.the-odds-api.com/v4"
ODDS_SPORT: str = "basketball_nba"
ODDS_REGIONS: str = "eu"
ODDS_MARKETS: str = "h2h,totals"
ODDS_ODDS_FORMAT: str = "decimal"

# ── NBA API ───────────────────────────────────────────────────────────────────
NBA_API_DELAY: float = float(os.getenv("NBA_API_DELAY", "1.0"))   # seconds between calls
NBA_API_RETRIES: int = int(os.getenv("NBA_API_RETRIES", "5"))
NBA_API_BACKOFF: float = float(os.getenv("NBA_API_BACKOFF", "2.0"))

# ── Database ──────────────────────────────────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
DB_PATH: str = os.path.join(DATA_DIR, "nba_predictions.db")

# ── Betting thresholds ────────────────────────────────────────────────────────
MIN_EDGE: float = float(os.getenv("MIN_EDGE", "0.05"))
MIN_PROBABILITY: float = float(os.getenv("MIN_PROBABILITY", "0.55"))

# ── Kelly Criterion ───────────────────────────────────────────────────────────
KELLY_FRACTION: float = float(os.getenv("KELLY_FRACTION", "0.25"))
MAX_STAKE: float = float(os.getenv("MAX_STAKE", "5.0"))
MIN_STAKE: float = float(os.getenv("MIN_STAKE", "0.5"))

# ── Model parameters ──────────────────────────────────────────────────────────
HOME_COURT_PTS: float = float(os.getenv("HOME_COURT_PTS", "2.5"))
B2B_PENALTY: float = float(os.getenv("B2B_PENALTY", "3.0"))
BASE_STD_DEV: float = float(os.getenv("BASE_STD_DEV", "12.0"))
SCORE_WEIGHT: float = float(os.getenv("SCORE_WEIGHT", "0.6"))
ELO_WEIGHT: float = float(os.getenv("ELO_WEIGHT", "0.4"))

# ── Season ────────────────────────────────────────────────────────────────────
CURRENT_SEASON: str = os.getenv("CURRENT_SEASON", "2025-26")
SEASON_TYPE: str = "Regular Season"

# ── Scheduler times (24h format) ─────────────────────────────────────────────
SCHEDULE_FETCH_TIME: str = os.getenv("SCHEDULE_FETCH_TIME", "10:00")
SCHEDULE_PREDICT_TIME: str = os.getenv("SCHEDULE_PREDICT_TIME", "22:00")
SCHEDULE_EVALUATE_TIME: str = os.getenv("SCHEDULE_EVALUATE_TIME", "09:00")

# ── NBA team abbreviation → full name ────────────────────────────────────────
NBA_TEAM_ABBREV: dict[str, str] = {
    "ATL": "Atlanta Hawks", "BOS": "Boston Celtics", "BKN": "Brooklyn Nets",
    "CHA": "Charlotte Hornets", "CHI": "Chicago Bulls", "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets", "DET": "Detroit Pistons",
    "GSW": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
    "LAC": "LA Clippers", "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies",
    "MIA": "Miami Heat", "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans", "NYK": "New York Knicks", "OKC": "Oklahoma City Thunder",
    "ORL": "Orlando Magic", "PHI": "Philadelphia 76ers", "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs",
    "TOR": "Toronto Raptors", "UTA": "Utah Jazz", "WAS": "Washington Wizards",
}

# Reverse lookup: full name → abbreviation
NBA_NAME_TO_ABBREV: dict[str, str] = {v: k for k, v in NBA_TEAM_ABBREV.items()}

# ── Props settings ────────────────────────────────────────────────────────────
PROPS_SEASON: str = os.getenv("PROPS_SEASON", "2025-26")
PROPS_RECENT_DAYS: int = int(os.getenv("PROPS_RECENT_DAYS", "20"))
PROPS_MIN_MINUTES: float = float(os.getenv("PROPS_MIN_MINUTES", "10.0"))
PROPS_MIN_GAMES: int = int(os.getenv("PROPS_MIN_GAMES", "5"))
PROPS_STD_DEV_FACTOR: float = float(os.getenv("PROPS_STD_DEV_FACTOR", "0.40"))
PROPS_B2B_FACTOR: float = float(os.getenv("PROPS_B2B_FACTOR", "0.92"))   # B2B game multiplier
PROPS_REST_BOOST: float = float(os.getenv("PROPS_REST_BOOST", "1.03"))   # 3+ days rest multiplier
PROPS_MAX_DAILY_EXPOSURE: float = float(os.getenv("PROPS_MAX_DAILY_EXPOSURE", "20.0"))  # max % bankroll/day
PROPS_ODDS_REGIONS: str = os.getenv("PROPS_ODDS_REGIONS", "us")
NBA_STATS_TIMEOUT: int = int(os.getenv("NBA_STATS_TIMEOUT", "60"))
NBA_STATS_DELAY: float = float(os.getenv("NBA_STATS_DELAY", "1.0"))
