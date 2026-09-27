import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_renders_both_prompts_into_out_dir(tmp_path):
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "render_worker_prompt.py"),
                          "--server", "http://10.0.0.5:8430/", "--home", "/tmp/fakehome", "--out-dir", str(tmp_path)],
                         capture_output=True, text=True, check=True).stdout
    deep = (tmp_path / "deep-dive-task.md").read_text()
    daily = (tmp_path / "daily-brief-task.md").read_text()
    assert "{{" not in deep and "{{" not in daily
    assert "http://10.0.0.5:8430/api/daily/claim" in daily and "/tmp/fakehome/.cache/morning-brief" in daily
    assert "Bash(curl *10.0.0.5:8430/api/daily/*)" in out and "Bash(curl *10.0.0.5:8430/api/deep-dives/*)" in out
