import os

import pytest


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """Point the loader at a private temp .env so tests never read the real one."""
    path = tmp_path / ".env"

    def write(text: str, mode: int = 0o600):
        path.write_text(text)
        os.chmod(path, mode)
        return path

    monkeypatch.setenv("TOURNAMENT_ENV_FILE", str(path))
    return write
