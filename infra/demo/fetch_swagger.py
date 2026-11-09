"""Fetch checksum-pinned Swagger UI assets for offline documentation."""

import argparse
import base64
import hashlib
import io
import tarfile
from pathlib import Path
from urllib.request import urlopen

URL = "https://registry.npmjs.org/swagger-ui-dist/-/swagger-ui-dist-5.34.1.tgz"
INTEGRITY = (
    "VSr8fXfI93obYaQicLJbLUKGRxDojj/SfRJDDLCioGTUBj4BK/Nj4eE9jCeCAnJx"
    "VLi7iS+08PbiZLyrQKBkJQ=="
)
MAX_DOWNLOAD = 20 * 1024 * 1024
ASSETS = {
    "swagger-ui-bundle.js",
    "swagger-ui.css",
    "favicon-32x32.png",
    "LICENSE",
}


def fetch(output):
    """Verify the whole npm archive and copy only named regular files."""
    with urlopen(URL, timeout=60) as response:
        content = response.read(MAX_DOWNLOAD + 1)
    if len(content) > MAX_DOWNLOAD:
        raise ValueError("Swagger UI archive exceeds the download limit.")
    expected = base64.b64decode(INTEGRITY, validate=True)
    if hashlib.sha512(content).digest() != expected:
        raise ValueError("Swagger UI archive checksum mismatch.")
    output.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
        for name in sorted(ASSETS):
            member = archive.getmember("package/" + name)
            if not member.isfile() or member.size > 8 * 1024 * 1024:
                raise ValueError("Unexpected Swagger UI archive member.")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("Swagger UI archive member is unreadable.")
            (output / name).write_bytes(stream.read())


def main():
    """Build assets without placing a node runtime in the API image."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path(__file__).parent / "static"
    )
    args = parser.parse_args()
    fetch(args.output)
    print("Verified Swagger UI 5.34.1 assets are ready for local serving.")


if __name__ == "__main__":
    main()
