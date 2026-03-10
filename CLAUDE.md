# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Automated, multi-agent NBA betting prediction system designed to run unattended on a Linux VPS. It fetches NBA data, models win probabilities, compares against bookmaker odds, and tracks P&L — all without human intervention.

## Running the System

**Manual phase execution:**
```bash
python main.py --phase fetch
python main.py --phase predict --date 2025-03-15
python main.py --phase evaluate
python main.py --phase all
```

**Scheduled (production):**
```bash
python scheduler.py   # Runs jobs at 09:00, 10:00, 22:00 daily
```

**Docker (recommended for production):**
```bash
docker compose up -d --build
docker compose logs -f
```

**Database inspection:**
```bash
sqlite3 data/nba_predictions.db
```

## Environment Setup

Copy `env.example` to `.env`. Required variable:
- `ODDS_API_KEY` — from https://the-odds-api.com/ (500 req/month free tier)

Key optional variables: `MIN_EDGE` (default 0.05), `MIN_PROBABILITY` (default 0.55), `CURRENT_SEASON` (e.g. `2024-25`), schedule times (`SCHEDULE_EVALUATE_TIME`, `SCHEDULE_FETCH_TIME`, `SCHEDULE_PREDICT_TIME`).

## Architecture

### Daily Pipeline (3 phases)

```
09:00 → AgentEvaluator    — compares yesterday's predictions to actual results, writes P&L
10:00 → AgentScout        — fetches team stats & schedule from NBA CDN, updates Elo & pace
22:00 → Full prediction:
          AgentScout           → fetch today's games
          AgentMatchupExpert   → Elo, home court (+2.5 pts), pace, H2H, recent form
          AgentMathematician   → normal distribution probabilities, score-based + Elo blend
          AgentOddsSpecialist  → fetch odds, devige, calculate Kelly stakes
          NBAOrchestrator      → save predictions to DB
```

### Agent Responsibilities

| Agent | File | Role |
|---|---|---|
| AgentScout | `agents/agent_scout.py` | NBA CDN data fetching, Elo ratings, pace, incremental game_log |
| AgentMatchupExpert | `agents/agent_matchup_expert.py` | Blends season (70%) + recent form (30%), H2H up to 10% weight |
| AgentMathematician | `agents/agent_mathematician.py` | Normal CDF on score diff; score-based (60%) + Elo-based (40%) blend |
| AgentOddsSpecialist | `agents/agent_odds_specialist.py` | The Odds API, devigging, Kelly Criterion (25% fraction, 0.5–5% stake) |
| AgentEvaluator | `agents/agent_evaluator.py` | Backtesting, outcome detection, P&L tracking |
| NBAOrchestrator | `main.py` | Phase coordination, argument parsing |
| Scheduler | `scheduler.py` | APScheduler job definitions |

### Database Schema (`data/nba_predictions.db`)

- `games` — game_id, date, teams, scores, status
- `team_stats` — Elo, pace, PPG, recent form, home/away splits (rebuilt from game_log each cycle)
- `game_log` — per-team per-game records; source for H2H and trend analysis
- `predictions` — model probabilities, odds, recommended bet, edge, `raw_data` JSON
- `bet_results` — outcome, P&L, stake per resolved prediction

### Data Sources

- **Schedule/scores**: `https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json`
- **Live scoreboard**: `https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json`
- **Boxscore**: `https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{game_id}.json`
- **Odds**: The Odds API (European format, NBA markets)

### Key Statistical Constants

- Home court advantage: **+2.5 points**
- Score std dev base: **12.0 points** (pace-adjusted: `std_dev * pace / 100`)
- Elo K-factor: FiveThirtyEight formula with margin-of-victory multiplier
- Pace estimation: `total_points / 2.15` → possessions per 48 min

## Infrastructure

- **Logging**: `logs/nba_system.log` — rotates at 10MB, 5 backups; format `YYYY-MM-DD HH:MM:SS | LEVEL | AGENT | msg`
- **SQLite**: WAL mode, foreign keys enabled, thread-safe via connection-per-call pattern
- **Docker**: 512M memory limit, 0.5 CPU, timezone Europe/Belgrade, persistent volumes `nba_data` + `nba_logs`
- **No test suite** — this is a production-only system with no automated tests
