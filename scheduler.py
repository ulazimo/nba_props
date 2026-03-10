"""
Scheduler
---------
Pokreće šest dnevnih zadataka:
  09:00 – Evaluacija rezultata prethodne noći (team + props)
  10:00 – Povlačenje podataka (team stats, schedule, player stats)
  22:00 – Generisanje finalnih tipova (team predictions + props)

Koristi APScheduler sa BlockingScheduler za rad bez nadzora.
Svaki zadatak je izolovan u try-except – pad jednog ne utiče na ostale.
"""

import sys
import os
import signal

sys.path.insert(0, os.path.dirname(__file__))

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

from config.logging_config import setup_logger
from config.settings import (
    SCHEDULE_FETCH_TIME,
    SCHEDULE_PREDICT_TIME,
    SCHEDULE_EVALUATE_TIME,
)
from main import NBAOrchestrator
from props_main import PropsOrchestrator

logger = setup_logger("Scheduler")

_team_orchestrator: NBAOrchestrator | None = None
_props_orchestrator: PropsOrchestrator | None = None


def _get_team_orchestrator() -> NBAOrchestrator:
    global _team_orchestrator
    if _team_orchestrator is None:
        _team_orchestrator = NBAOrchestrator()
    return _team_orchestrator


def _get_props_orchestrator() -> PropsOrchestrator:
    global _props_orchestrator
    if _props_orchestrator is None:
        _props_orchestrator = PropsOrchestrator()
    return _props_orchestrator


# ── Team prediction jobs ───────────────────────────────────────────────────────

def job_fetch_data() -> None:
    logger.info("▶ Scheduled job: DATA FETCH triggered")
    try:
        _get_team_orchestrator().run_data_fetch()
    except Exception as exc:
        logger.error("job_fetch_data crashed: %s", exc, exc_info=True)


def job_generate_predictions() -> None:
    logger.info("▶ Scheduled job: PREDICTION GENERATION triggered")
    try:
        _get_team_orchestrator().run_prediction()
    except Exception as exc:
        logger.error("job_generate_predictions crashed: %s", exc, exc_info=True)


def job_evaluate() -> None:
    logger.info("▶ Scheduled job: EVALUATION triggered")
    try:
        _get_team_orchestrator().run_evaluation()
    except Exception as exc:
        logger.error("job_evaluate crashed: %s", exc, exc_info=True)


# ── Props jobs ─────────────────────────────────────────────────────────────────

def job_fetch_props() -> None:
    logger.info("▶ Scheduled job: PROPS DATA FETCH triggered")
    try:
        _get_props_orchestrator().run_props_fetch()
    except Exception as exc:
        logger.error("job_fetch_props crashed: %s", exc, exc_info=True)


def job_generate_props_predictions() -> None:
    logger.info("▶ Scheduled job: PROPS PREDICTION GENERATION triggered")
    try:
        _get_props_orchestrator().run_props_predict()
    except Exception as exc:
        logger.error("job_generate_props_predictions crashed: %s", exc, exc_info=True)


def job_evaluate_props() -> None:
    logger.info("▶ Scheduled job: PROPS EVALUATION triggered")
    try:
        _get_props_orchestrator().run_props_evaluate()
    except Exception as exc:
        logger.error("job_evaluate_props crashed: %s", exc, exc_info=True)


# ── APScheduler event listener ────────────────────────────────────────────────

def _job_listener(event) -> None:
    if event.exception:
        logger.error(
            "Job '%s' raised an exception: %s", event.job_id, event.exception
        )
    else:
        logger.info("Job '%s' completed successfully", event.job_id)


# ── Scheduler setup ───────────────────────────────────────────────────────────

def _parse_time(time_str: str) -> tuple[int, int]:
    h, m = time_str.split(":")
    return int(h), int(m)


def build_scheduler() -> BlockingScheduler:
    sched = BlockingScheduler(timezone="Europe/Belgrade")
    sched.add_listener(_job_listener, EVENT_JOB_ERROR | EVENT_JOB_EXECUTED)

    eval_h, eval_m = _parse_time(SCHEDULE_EVALUATE_TIME)
    fetch_h, fetch_m = _parse_time(SCHEDULE_FETCH_TIME)
    pred_h, pred_m = _parse_time(SCHEDULE_PREDICT_TIME)

    # Team prediction jobs
    sched.add_job(
        job_evaluate, trigger="cron", hour=eval_h, minute=eval_m,
        id="job_evaluate", name="Team Evaluation",
        misfire_grace_time=600, coalesce=True,
    )
    sched.add_job(
        job_fetch_data, trigger="cron", hour=fetch_h, minute=fetch_m,
        id="job_fetch_data", name="Team Data Fetch",
        misfire_grace_time=600, coalesce=True,
    )
    sched.add_job(
        job_generate_predictions, trigger="cron", hour=pred_h, minute=pred_m,
        id="job_generate_predictions", name="Team Prediction Generation",
        misfire_grace_time=600, coalesce=True,
    )

    # Props jobs (same schedule, run after team jobs via separate IDs)
    sched.add_job(
        job_evaluate_props, trigger="cron", hour=eval_h, minute=eval_m,
        id="job_evaluate_props", name="Props Evaluation",
        misfire_grace_time=600, coalesce=True,
    )
    sched.add_job(
        job_fetch_props, trigger="cron", hour=fetch_h, minute=fetch_m,
        id="job_fetch_props", name="Props Data Fetch",
        misfire_grace_time=600, coalesce=True,
    )
    sched.add_job(
        job_generate_props_predictions, trigger="cron", hour=pred_h, minute=pred_m,
        id="job_generate_props_predictions", name="Props Prediction Generation",
        misfire_grace_time=600, coalesce=True,
    )

    return sched


# ── Graceful shutdown ─────────────────────────────────────────────────────────

_scheduler_ref: BlockingScheduler | None = None


def _handle_signal(signum, frame) -> None:
    logger.info("Received signal %d – shutting down scheduler gracefully", signum)
    if _scheduler_ref:
        _scheduler_ref.shutdown(wait=False)
    sys.exit(0)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("NBA Prediction Scheduler starting")
    logger.info(
        "Schedule: evaluate=%s | fetch=%s | predict=%s",
        SCHEDULE_EVALUATE_TIME,
        SCHEDULE_FETCH_TIME,
        SCHEDULE_PREDICT_TIME,
    )
    logger.info("=" * 60)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    _scheduler_ref = build_scheduler()

    try:
        _scheduler_ref.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")
