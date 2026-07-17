"""Build the Skyfield Lambda layer zip.

Installs skyfield and its dependencies (numpy, sgp4, jplephem, certifi) as
manylinux wheels targeting the Lambda python3.12 runtime — wheels built for
Windows/macOS won't import on Lambda, which is why this can't just zip the
local venv. Also bundles the de421 JPL ephemeris, which the pass-visibility
math needs for sun positions (plain position propagation is ephemeris-free).

Layer zip layout:
    python/...        -> /opt/python (site-packages, on sys.path in Lambda)
    data/de421.bsp    -> /opt/data/de421.bsp (EPHEMERIS_PATH env var)

The pip/work directory lives in the system temp dir, not the repo — this
repo syncs to OneDrive, and a site-packages tree is thousands of small files
that OneDrive would dutifully upload. Only the single zip lands in dist/.
"""

import hashlib
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
DIST = HERE / "dist"
PYTHON_VERSION = "3.12"
EPHEMERIS_URL = (
    "https://naif.jpl.nasa.gov/pub/naif/generic_kernels/spk/planets/"
    "a_old_versions/de421.bsp"
)


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="skyfield-layer-"))
    site = work / "python"

    subprocess.run(
        [
            sys.executable, "-m", "pip", "install",
            "--target", str(site),
            "--platform", "manylinux2014_x86_64",
            "--implementation", "cp",
            "--python-version", PYTHON_VERSION,
            "--only-binary=:all:",
            "-r", str(HERE / "requirements.txt"),
        ],
        check=True,
    )

    ephemeris = work / "data" / "de421.bsp"
    ephemeris.parent.mkdir()
    print(f"Downloading {EPHEMERIS_URL} ...")
    urllib.request.urlretrieve(EPHEMERIS_URL, ephemeris)

    DIST.mkdir(exist_ok=True)
    zip_path = DIST / "skyfield-layer.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(work.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                zf.write(path, path.relative_to(work))

    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()[:16]
    size_mb = zip_path.stat().st_size / 1024 / 1024
    print(f"Wrote {zip_path} ({size_mb:.1f} MB, sha256 {digest}...)")


if __name__ == "__main__":
    main()
