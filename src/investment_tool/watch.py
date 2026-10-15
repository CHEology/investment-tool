"""Local paper-observation journal. Repeated dates cannot produce duplicate alerts."""

import fcntl
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from .data import DataError, sessions
from .storage import digest, read_json, write_json


@contextmanager
def locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DataError("Another observation is updating this journal") from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def expected_session(now: pd.Timestamp | None = None) -> str:
    import exchange_calendars as xcals

    now = pd.Timestamp.now(tz="UTC") if now is None else now.tz_convert("UTC")
    start, end = (now - pd.Timedelta(days=14)).date().isoformat(), now.date().isoformat()
    calendar = xcals.get_calendar("XNYS", start=start, end=end)
    eligible = [
        day
        for day in sessions(start, end)
        if calendar.session_close(day) + pd.Timedelta(hours=2) <= now
    ]
    if not eligible:
        raise DataError("No completed US session")
    return eligible[-1].strftime("%Y-%m-%d")


def observe(run: Path, root: Path, now: pd.Timestamp | None = None) -> Path:
    """Persist watchlist changes only; no transport or trading integration."""
    manifest = read_json(run / "manifest.json")
    if manifest.get("status") != "completed" or manifest.get("synthetic"):
        raise DataError("Only completed, non-synthetic runs can enter the paper journal")
    for filename in ("ranking.json", "evaluation.json"):
        if digest(run / filename) != manifest["artifacts"][filename]:
            raise DataError("Research artifact hash mismatch")
    ranking = read_json(run / "ranking.json")
    expected = expected_session(now)
    if ranking["as_of"] != expected:
        raise DataError(f"Observation requires session {expected}, got {ranking['as_of']}")
    state_dir = root / "paper"
    with locked(state_dir / ".lock"):
        config_key = {
            "config": manifest["config"],
            "symbols": sorted(c["symbol"] for c in ranking["candidates"]),
        }
        journal_path = state_dir / "journal.json"
        journal = (
            read_json(journal_path)
            if journal_path.exists()
            else {"experiment": config_key, "observations": []}
        )
        if journal["experiment"] != config_key:
            raise DataError("Use a separate --root for a different universe or configuration")
        observations = journal["observations"]
        date = ranking["as_of"]
        previous = observations[-1] if observations else None
        if previous and date <= previous["as_of"]:
            if date == previous["as_of"]:
                return journal_path
            raise DataError("Cannot move the paper journal backwards")
        candidates = ranking["candidates"]
        if all("selected" in c for c in candidates):
            # Tied cutoff scores can select more than top_k names.
            top = [c["symbol"] for c in candidates if c["selected"]]
        else:
            top = [c["symbol"] for c in candidates[: manifest["config"]["top_k"]]]
        old_top = previous["top"] if previous else []
        observations.append(
            {
                "as_of": date,
                "run_id": run.name,
                "top": top,
                "entered": sorted(set(top) - set(old_top)),
                "left": sorted(set(old_top) - set(top)),
                "baseline": previous is None,
            }
        )
        write_json(journal_path, journal)
        return journal_path
