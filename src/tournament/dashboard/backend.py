"""Wires the dashboard to its sources: the config, the API with the right tokens, the folders.

Tests replace `override`; the app calls `get_backend()` and doesn't care which it gets.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tournament.dashboard.config import REPO_ROOT, DashboardConfig, load_dashboard_config
from tournament.dashboard.data import ApiClient
from tournament.secrets import InsecureEnvFileError, MissingCredentialsError, load_api_tokens


@dataclass
class Backend:
    cfg: DashboardConfig
    api: ApiClient | None
    setup_error: str | None
    root: Path = REPO_ROOT

    @property
    def live_root(self) -> Path:
        return self.root / self.cfg.data.live_root

    @property
    def ops_root(self) -> Path:
        return self.root / self.cfg.data.ops_root


override: Backend | None = None


def get_backend() -> Backend:
    if override is not None:
        return override
    cfg = load_dashboard_config()
    try:
        t = load_api_tokens()
    except (MissingCredentialsError, InsecureEnvFileError) as exc:
        return Backend(cfg, None, str(exc))
    return Backend(cfg, ApiClient(cfg.api.base_url, cfg.api.timeout_s, t.viewer, t.owner), None)
