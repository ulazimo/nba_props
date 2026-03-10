"""
Database layer – SQLite with thread-safe connection management.
All DDL and DML operations live here so agents stay clean.
"""

import sqlite3
import os
import json
from contextlib import contextmanager
from datetime import date
from typing import Generator, Optional

from config.settings import DB_PATH
from config.logging_config import setup_logger

logger = setup_logger("database")


def _ensure_data_dir() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


@contextmanager
def get_connection() -> Generator[sqlite3.Connection, None, None]:
    _ensure_data_dir()
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize_db() -> None:
    logger.info("Initializing database at %s", DB_PATH)
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS games (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id         TEXT    NOT NULL UNIQUE,
                game_date       DATE    NOT NULL,
                home_team       TEXT    NOT NULL,
                away_team       TEXT    NOT NULL,
                home_score      INTEGER,
                away_score      INTEGER,
                status          TEXT    DEFAULT 'scheduled',
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS team_stats (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id         TEXT    NOT NULL,
                team_name       TEXT    NOT NULL,
                season          TEXT    NOT NULL,
                games_played    INTEGER,
                off_rating      REAL,
                def_rating      REAL,
                pace            REAL,
                pts_per_game    REAL,
                opp_pts_per_game REAL,
                home_ppg        REAL,
                away_ppg        REAL,
                home_opp_ppg    REAL,
                away_opp_ppg    REAL,
                home_wins       INTEGER DEFAULT 0,
                home_losses     INTEGER DEFAULT 0,
                away_wins       INTEGER DEFAULT 0,
                away_losses     INTEGER DEFAULT 0,
                last10_ppg      REAL,
                last10_opp_ppg  REAL,
                last10_wins     INTEGER DEFAULT 0,
                last10_losses   INTEGER DEFAULT 0,
                elo_rating      REAL    DEFAULT 1500.0,
                streak          INTEGER DEFAULT 0,
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(team_id, season)
            );

            CREATE TABLE IF NOT EXISTS game_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id         TEXT    NOT NULL,
                game_date       DATE    NOT NULL,
                team_id         TEXT    NOT NULL,
                team_name       TEXT    NOT NULL,
                opponent_id     TEXT    NOT NULL,
                opponent_name   TEXT    NOT NULL,
                is_home         INTEGER NOT NULL,
                team_score      INTEGER NOT NULL,
                opp_score       INTEGER NOT NULL,
                pace            REAL,
                season          TEXT    NOT NULL,
                UNIQUE(game_id, team_id)
            );

            CREATE TABLE IF NOT EXISTS predictions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id         TEXT    NOT NULL REFERENCES games(game_id),
                predicted_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                home_win_prob   REAL,
                away_win_prob   REAL,
                predicted_total REAL,
                total_line      REAL,
                home_odds       REAL,
                away_odds       REAL,
                total_over_odds REAL,
                total_under_odds REAL,
                edge_home       REAL,
                edge_away       REAL,
                edge_over       REAL,
                edge_under      REAL,
                recommended_bet TEXT,
                bet_stake       REAL,
                raw_data        TEXT
            );

            CREATE TABLE IF NOT EXISTS bet_results (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                prediction_id   INTEGER NOT NULL REFERENCES predictions(id),
                game_id         TEXT    NOT NULL,
                evaluated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                bet_type        TEXT,
                bet_odds        REAL,
                stake           REAL,
                outcome         TEXT,
                profit_loss     REAL,
                notes           TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_games_date     ON games(game_date);
            CREATE INDEX IF NOT EXISTS idx_games_status    ON games(status);
            CREATE INDEX IF NOT EXISTS idx_pred_game       ON predictions(game_id);
            CREATE INDEX IF NOT EXISTS idx_gamelog_team     ON game_log(team_id, season);
            CREATE INDEX IF NOT EXISTS idx_gamelog_date     ON game_log(game_date);
            CREATE INDEX IF NOT EXISTS idx_gamelog_matchup  ON game_log(team_id, opponent_id, season);
            """
        )
    logger.info("Database initialized successfully")


# ── Games ─────────────────────────────────────────────────────────────────────

def upsert_game(game_id: str, game_date: str, home_team: str, away_team: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO games (game_id, game_date, home_team, away_team)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(game_id) DO UPDATE SET
                game_date  = excluded.game_date,
                home_team  = excluded.home_team,
                away_team  = excluded.away_team
            """,
            (game_id, game_date, home_team, away_team),
        )


def update_game_result(game_id: str, home_score: int, away_score: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE games SET home_score=?, away_score=?, status='final' WHERE game_id=?",
            (home_score, away_score, game_id),
        )


def get_games_for_date(game_date: str) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM games WHERE game_date=? ORDER BY game_id",
            (game_date,),
        ).fetchall()


def get_finished_games_without_evaluation() -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT DISTINCT g.* FROM games g
            JOIN predictions p ON p.game_id = g.game_id
            LEFT JOIN bet_results br ON br.game_id = g.game_id
            WHERE g.status = 'final' AND br.id IS NULL
            """,
        ).fetchall()


# ── Game log ──────────────────────────────────────────────────────────────────

def upsert_game_log(entry: dict) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO game_log
                (game_id, game_date, team_id, team_name, opponent_id, opponent_name,
                 is_home, team_score, opp_score, pace, season)
            VALUES
                (:game_id, :game_date, :team_id, :team_name, :opponent_id, :opponent_name,
                 :is_home, :team_score, :opp_score, :pace, :season)
            ON CONFLICT(game_id, team_id) DO UPDATE SET
                team_score = excluded.team_score,
                opp_score  = excluded.opp_score,
                pace       = excluded.pace
            """,
            entry,
        )


def get_team_game_log(team_name: str, season: str, limit: int = 82) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT * FROM game_log
            WHERE team_name=? AND season=?
            ORDER BY game_date DESC
            LIMIT ?
            """,
            (team_name, season, limit),
        ).fetchall()


def get_last_game_date(team_name: str, before_date: str) -> Optional[str]:
    """Returns the most recent game_date for a team strictly before before_date."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT game_date FROM game_log
            WHERE team_name = ? AND game_date < ?
            ORDER BY game_date DESC
            LIMIT 1
            """,
            (team_name, before_date),
        ).fetchone()
        return str(row["game_date"]) if row else None


def get_h2h_games(team_name: str, opponent_name: str, max_games: int = 10) -> list[sqlite3.Row]:
    """Returns H2H games across all seasons (most recent first)."""
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT * FROM game_log
            WHERE team_name=? AND opponent_name=?
            ORDER BY game_date DESC
            LIMIT ?
            """,
            (team_name, opponent_name, max_games),
        ).fetchall()


# ── Team stats ────────────────────────────────────────────────────────────────

def upsert_team_stats(stats: dict) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO team_stats
                (team_id, team_name, season, games_played, off_rating, def_rating,
                 pace, pts_per_game, opp_pts_per_game,
                 home_ppg, away_ppg, home_opp_ppg, away_opp_ppg,
                 home_wins, home_losses, away_wins, away_losses,
                 last10_ppg, last10_opp_ppg, last10_wins, last10_losses,
                 elo_rating, streak)
            VALUES (:team_id, :team_name, :season, :games_played, :off_rating,
                    :def_rating, :pace, :pts_per_game, :opp_pts_per_game,
                    :home_ppg, :away_ppg, :home_opp_ppg, :away_opp_ppg,
                    :home_wins, :home_losses, :away_wins, :away_losses,
                    :last10_ppg, :last10_opp_ppg, :last10_wins, :last10_losses,
                    :elo_rating, :streak)
            ON CONFLICT(team_id, season) DO UPDATE SET
                team_name        = excluded.team_name,
                games_played     = excluded.games_played,
                off_rating       = excluded.off_rating,
                def_rating       = excluded.def_rating,
                pace             = excluded.pace,
                pts_per_game     = excluded.pts_per_game,
                opp_pts_per_game = excluded.opp_pts_per_game,
                home_ppg         = excluded.home_ppg,
                away_ppg         = excluded.away_ppg,
                home_opp_ppg     = excluded.home_opp_ppg,
                away_opp_ppg     = excluded.away_opp_ppg,
                home_wins        = excluded.home_wins,
                home_losses      = excluded.home_losses,
                away_wins        = excluded.away_wins,
                away_losses      = excluded.away_losses,
                last10_ppg       = excluded.last10_ppg,
                last10_opp_ppg   = excluded.last10_opp_ppg,
                last10_wins      = excluded.last10_wins,
                last10_losses    = excluded.last10_losses,
                elo_rating       = excluded.elo_rating,
                streak           = excluded.streak,
                updated_at       = CURRENT_TIMESTAMP
            """,
            stats,
        )


def get_team_stats(team_name: str, season: str) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM team_stats WHERE team_name=? AND season=?",
            (team_name, season),
        ).fetchone()


def get_all_team_stats(season: str) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM team_stats WHERE season=?", (season,)
        ).fetchall()


def get_games_pending_results() -> list[str]:
    """Returns game_ids that have predictions but no final score yet.
    Limited to last 7 days to avoid chasing postponed/cancelled games.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT g.game_id FROM games g
            JOIN predictions p ON p.game_id = g.game_id
            WHERE g.status != 'final'
              AND g.game_date < date('now')
              AND g.game_date >= date('now', '-7 days')
            """,
        ).fetchall()
        return [r["game_id"] for r in rows]


def get_processed_game_ids(season: str) -> set[str]:
    """Returns set of game_ids already present in game_log for this season."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT game_id FROM game_log WHERE season=?",
            (season,),
        ).fetchall()
        return {r["game_id"] for r in rows}


# ── Predictions ───────────────────────────────────────────────────────────────

def save_prediction(pred: dict) -> int:
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO predictions
                (game_id, home_win_prob, away_win_prob, predicted_total,
                 total_line,
                 home_odds, away_odds, total_over_odds, total_under_odds,
                 edge_home, edge_away, edge_over, edge_under,
                 recommended_bet, bet_stake, raw_data)
            VALUES
                (:game_id, :home_win_prob, :away_win_prob, :predicted_total,
                 :total_line,
                 :home_odds, :away_odds, :total_over_odds, :total_under_odds,
                 :edge_home, :edge_away, :edge_over, :edge_under,
                 :recommended_bet, :bet_stake, :raw_data)
            """,
            {**pred, "raw_data": json.dumps(pred.get("raw_data", {}))},
        )
        return cursor.lastrowid


def get_predictions_for_game(game_id: str) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM predictions WHERE game_id=? ORDER BY predicted_at DESC",
            (game_id,),
        ).fetchall()


# ── Bet results ───────────────────────────────────────────────────────────────

def save_bet_result(result: dict) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO bet_results
                (prediction_id, game_id, bet_type, bet_odds, stake, outcome, profit_loss, notes)
            VALUES
                (:prediction_id, :game_id, :bet_type, :bet_odds, :stake, :outcome, :profit_loss, :notes)
            """,
            result,
        )


def get_profitability_summary() -> dict:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*)                                    AS total_bets,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) AS losses,
                ROUND(SUM(profit_loss), 2)                  AS total_profit,
                ROUND(SUM(CASE WHEN outcome != 'void' THEN stake ELSE 0 END), 2) AS total_staked,
                ROUND(AVG(profit_loss), 2)                  AS avg_profit_per_bet
            FROM bet_results
            """
        ).fetchone()
        if not row:
            return {}
        d = dict(row)
        # SQLite SUM() returns NULL when there are no rows — normalize to 0
        d["wins"] = d["wins"] or 0
        d["losses"] = d["losses"] or 0
        d["total_profit"] = d["total_profit"] or 0.0
        d["total_staked"] = d["total_staked"] or 0.0
        d["avg_profit_per_bet"] = d["avg_profit_per_bet"] or 0.0
        staked = d["total_staked"]
        d["roi_pct"] = round((d["total_profit"] / staked * 100), 2) if staked else 0.0

        # Breakdown by bet type
        type_rows = conn.execute(
            """
            SELECT
                bet_type,
                COUNT(*)                                        AS bets,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END)  AS wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) AS losses,
                ROUND(SUM(profit_loss), 2)                      AS profit,
                ROUND(SUM(CASE WHEN outcome != 'void' THEN stake ELSE 0 END), 2) AS staked
            FROM bet_results
            GROUP BY bet_type
            """
        ).fetchall()
        d["by_bet_type"] = {
            r["bet_type"]: {
                "bets": r["bets"],
                "wins": r["wins"],
                "losses": r["losses"],
                "profit": r["profit"],
                "roi_pct": round(r["profit"] / r["staked"] * 100, 2) if r["staked"] else 0.0,
            }
            for r in type_rows if r["bet_type"]
        }
        return d


def get_calibration_data() -> list[dict]:
    """Returns (our_prob, outcome) for all evaluated bets with known probability."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                CASE p.recommended_bet
                    WHEN 'home_win' THEN p.home_win_prob
                    WHEN 'away_win' THEN p.away_win_prob
                    WHEN 'over'  THEN CAST(json_extract(p.raw_data, '$.probabilities.over_prob')  AS REAL)
                    WHEN 'under' THEN CAST(json_extract(p.raw_data, '$.probabilities.under_prob') AS REAL)
                    ELSE NULL
                END AS our_prob,
                br.outcome
            FROM predictions p
            JOIN bet_results br ON br.prediction_id = p.id
            WHERE br.outcome IN ('win', 'loss')
              AND p.recommended_bet IS NOT NULL
            """,
        ).fetchall()
        return [dict(r) for r in rows]
