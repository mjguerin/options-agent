"""Render the Supervisor config and the launchd job files from config/app (D-59).

The times, retries and waits are values in the YAML, never in the templates. The output goes to
a directory; `ops/host/setup_jobs.sh` installs it. Nothing here touches the system.
"""

from __future__ import annotations

from pathlib import Path

from tournament.appconfig import AppConfig

TEMPLATES = Path(__file__).resolve().parents[3] / "ops" / "templates"
WEEKDAYS = (1, 2, 3, 4, 5)  # launchd: Monday = 1 ... Friday = 5


def calendar_xml(hhmm: str, weekdays_only: bool) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    days = WEEKDAYS if weekdays_only else (None,)
    out = []
    for d in days:
        wd = f"\t\t\t<key>Weekday</key><integer>{d}</integer>\n" if d is not None else ""
        out.append(
            "\t\t<dict>\n"
            f"{wd}\t\t\t<key>Hour</key><integer>{h}</integer>\n"
            f"\t\t\t<key>Minute</key><integer>{m}</integer>\n"
            "\t\t</dict>"
        )
    return "\n".join(out)


def values(cfg: AppConfig, owner: str, home: Path, repo: Path, uv: str) -> dict[str, str]:
    return {
        "__USER__": owner,
        "__HOME__": str(home),
        "__REPO__": str(repo),
        "__UV__": uv,
        "__RUN__": str(repo / "data" / "run"),
        "__LOGS__": str(home / "Library" / "Logs" / "options-tournament"),
        "__STARTSECS__": str(cfg.engine.startsecs),
        "__RETRIES__": str(cfg.engine.restart_attempts),
        "__STOPWAIT__": str(cfg.engine.stop_wait_s),
        "__ENGINE_CALENDAR__": calendar_xml(cfg.engine.launch_time, True),
        "__NIGHTLY_CALENDAR__": calendar_xml(cfg.nightly.start_time, cfg.nightly.weekdays_only),
    }


def render_all(
    cfg: AppConfig, owner: str, home: Path, repo: Path, uv: str, out: Path
) -> list[Path]:
    v = values(cfg, owner, home, repo, uv)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for tmpl in sorted(TEMPLATES.glob("*.tmpl")):
        text = tmpl.read_text()
        for k, val in v.items():
            text = text.replace(k, val)
        left = [w for w in text.split() if w.startswith("__") and w.endswith("__")]
        if left:
            raise ValueError(f"{tmpl.name}: unfilled placeholders {left}")
        dest = out / tmpl.name.removesuffix(".tmpl")
        dest.write_text(text)
        written.append(dest)
    return written
