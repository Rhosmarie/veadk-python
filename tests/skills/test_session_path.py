import platform
import tempfile
from pathlib import Path

from veadk.tools.skills_tools.session_path import (
    clear_session_cache,
    initialize_session_path,
)


def test_initialize_session_path_uses_default_base(monkeypatch):
    session_id = "session-default"
    monkeypatch.delenv("VEADK_SKILLS_WORK_DIR", raising=False)
    clear_session_cache()

    session_path = initialize_session_path(session_id)

    if platform.system() in ("Linux", "Darwin"):
        expected_base_path = Path("/tmp") / "veadk"
    else:
        expected_base_path = Path(tempfile.gettempdir()) / "veadk"
    assert session_path == expected_base_path / session_id
    assert (session_path / "skills").is_dir()
    assert (session_path / "uploads").is_dir()
    assert (session_path / "outputs").is_dir()

    clear_session_cache()


def test_initialize_session_path_uses_configured_work_dir(tmp_path, monkeypatch):
    session_id = "session-custom"
    configured_base_path = tmp_path / "custom-work-dir"
    monkeypatch.setenv("VEADK_SKILLS_WORK_DIR", str(configured_base_path))
    clear_session_cache()

    session_path = initialize_session_path(session_id)

    assert session_path == configured_base_path / session_id
    assert (session_path / "skills").is_dir()
    assert (session_path / "uploads").is_dir()
    assert (session_path / "outputs").is_dir()

    clear_session_cache()
