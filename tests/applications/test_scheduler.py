"""Exercise the real cron wrapper with isolated fake runtimes, never live polling."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("poll_exit,pipeline_exit,expected", [(3, 0, 3), (0, 7, 7), (0, 0, 0)])
def test_scheduler_runs_both_stages_with_minimal_launchd_path(tmp_path, poll_exit, pipeline_exit, expected):
    root = Path(__file__).resolve().parents[2]
    shutil.copy2(root / "run_poll.sh", tmp_path / "run_poll.sh")
    home = tmp_path / "fake-home"
    poll_python = home / "miniforge3/envs/jhb/bin/python"
    pipeline_python = tmp_path / ".venv/bin/python"
    for path, stage, code in [(poll_python, "poll", poll_exit), (pipeline_python, "pipeline", pipeline_exit)]:
        path.parent.mkdir(parents=True)
        path.write_text(
            "#!/bin/bash\n"
            f'printf "%s\\n" "$PATH" > "{tmp_path}/{stage}.path"\n'
            f'printf "%s\\n" "$*" > "{tmp_path}/{stage}.args"\n'
            f"exit {code}\n"
        )
        path.chmod(0o700)
    result = subprocess.run(
        ["/bin/bash", str(tmp_path / "run_poll.sh")],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == expected
    assert (tmp_path / "poll.args").read_text().strip() == "-m jhb.poll"
    assert (tmp_path / "pipeline.args").read_text().strip() == "-m jhb.applications.cli pipeline --if-enabled"
    for stage in ["poll", "pipeline"]:
        paths = (tmp_path / f"{stage}.path").read_text().strip().split(os.pathsep)
        assert str(home / ".local/bin") in paths
        assert "/opt/homebrew/bin" in paths
    assert (tmp_path / "data/poll.log").exists()
    assert (tmp_path / "data/applications.log").exists()
