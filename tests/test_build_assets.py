import subprocess
import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_build_assets_writes_public(tmp_path):
    out = tmp_path / "public"
    env = os.environ.copy()
    env["BUILD_OUTPUT_DIR"] = str(out)
    env["TIMETAGGER_PATH_PREFIX"] = "/"
    env["DATABASE_URL"] = os.environ.get("DATABASE_URL", "")
    env["PYTHONPATH"] = str(REPO)
    res = subprocess.run(
        [str(REPO / ".venv" / "bin" / "python"), "scripts/build_assets.py"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"STDOUT: {res.stdout}\nSTDERR: {res.stderr}"
    assert (out / "app").is_dir()
    files = list(out.rglob("*"))
    exts = {f.suffix for f in files if f.is_file()}
    assert ".html" in exts, f"No .html files found. Extensions: {exts}"
    assert ".js" in exts, f"No .js files found. Extensions: {exts}"
    # service worker in app/
    app_files = list((out / "app").iterdir())
    app_names = {f.name for f in app_files}
    assert "sw.js" in app_names, f"sw.js not found in app/. Files: {app_names}"
