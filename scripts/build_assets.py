"""Pre-compile TimeTagger client assets into a directory for static hosting."""
import os
import sys
from importlib import resources
from pathlib import Path

# Ensure the repo root is on sys.path so `timetagger` is importable even when
# the package is not pip-installed (e.g., in Vercel's build environment).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TIMETAGGER_PATH_PREFIX", "/")

from timetagger.server import create_assets_from_dir, enable_service_worker  # noqa: E402


def write_assets(out_dir: Path, assets: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, content in assets.items():
        # Extensionless HTML pages: write as <name>/index.html for clean URLs,
        # or index.html at root for the empty-string key.
        if name == "":
            path = out_dir / "index.html"
        elif "." not in name:
            path = out_dir / name / "index.html"
        else:
            path = out_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        elif isinstance(content, str):
            path.write_text(content)
        elif isinstance(content, tuple):
            # asgineer may wrap as (content_type, bytes_or_str)
            _ct, body = content
            if isinstance(body, bytes):
                path.write_bytes(body)
            else:
                path.write_text(str(body))
        else:
            raise TypeError(f"Unknown asset type for {name}: {type(content)}")


def main() -> int:
    out = Path(os.environ.get("BUILD_OUTPUT_DIR", "public")).resolve()
    if out.exists():
        import shutil

        shutil.rmtree(out)

    common = create_assets_from_dir(resources.files("timetagger.common"))
    apponly = create_assets_from_dir(resources.files("timetagger.app"))
    image = create_assets_from_dir(resources.files("timetagger.images"))
    page = create_assets_from_dir(resources.files("timetagger.pages"))

    app_assets = {**common, **image, **apponly}
    web_assets = {**common, **image, **page}
    enable_service_worker(app_assets)

    write_assets(out / "app", app_assets)
    write_assets(out, web_assets)
    count = sum(1 for _ in out.rglob("*") if _.is_file())
    print(f"wrote {count} files to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
