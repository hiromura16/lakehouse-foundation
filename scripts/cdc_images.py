#!/usr/bin/env python3
"""Generate CDC image references and validate public registry artifacts.

The default check is offline and reads only public lock/Compose/Dockerfile
sources. --write-compose updates image lines from the CDC lock; regenerate
the inventory separately with compose_inventory.py. --registry uses daemon-
free buildx reads for tags, pinned indexes/manifests and linux/amd64 configs.
--pull-smoke additionally downloads each immutable image to the caller's
authorized Docker daemon and verifies its platform and RepoDigests. It never
starts containers, changes daemon permissions, or reads deployment settings.
"""

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = Path("infra/cdc/compose.yaml")
SERVICES = {
    "kafka": "cdc-kafka",
    "debezium": "cdc-connect",
    "kafbat": "cdc-ui",
}
DIGEST = r"sha256:[0-9a-f]{64}"
PIN = re.compile(
    r"(?P<repository>[a-z0-9][a-z0-9./_-]*)"
    r":(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]*)@(?P<digest>" + DIGEST + r")"
)
INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
MANIFEST_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
ATTEMPTS = 3


class ImageError(ValueError):
    """Report fixed diagnostics without dumping local Docker configuration."""


def parse_pin(reference):
    """Require an immutable digest and a version tag for comparison."""
    match = PIN.fullmatch(reference) if isinstance(reference, str) else None
    if not match or reference.startswith("-") or "/" not in reference:
        raise ImageError("IMAGE_PIN_INVALID")
    return match.groupdict()


def load_images(root):
    """Load service pins and shared build inputs, rejecting coverage gaps."""
    locks = json.loads((root / "infra/cdc/images.lock.json").read_text())
    if set(locks) != set(SERVICES):
        raise ImageError("CDC_LOCK_COVERAGE_MISMATCH")
    images = {name: entry["pinned"] for name, entry in locks.items()}
    for name, source in (("python", "lakehouse"), ("uv", "uv")):
        shared = json.loads(
            (root / f"infra/{source}/images.lock.json").read_text()
        )
        images[name] = shared[name]["pinned"]
    for reference in images.values():
        parse_pin(reference)
    dockerfile = (root / "infra/cdc/Dockerfile").read_text()
    build_images = dockerfile_images(dockerfile)
    if sorted(build_images) != sorted([images["python"], images["uv"]]):
        raise ImageError("CDC_DOCKERFILE_LOCK_MISMATCH")
    return images


def dockerfile_images(source):
    """Collect FROM/COPY inputs regardless of case, flags or continuation.

    Internal build stages would need an explicit coverage policy too. Reject
    extra inputs rather than silently assuming a stage name is local.
    """
    references = []
    for line in re.sub(r"\\\r?\n", " ", source).splitlines():
        if not re.match(r"^\s*(FROM|COPY)\s", line, re.IGNORECASE):
            continue
        tokens = shlex.split(line, comments=True)
        instruction = tokens.pop(0).upper()
        if instruction == "FROM":
            arguments = [
                value for value in tokens if not value.startswith("--")
            ]
            if not arguments:
                raise ImageError("CDC_DOCKERFILE_INPUT_INVALID")
            references.append(arguments[0])
        else:
            references.extend(
                value.split("=", 1)[1]
                for value in tokens
                if value.lower().startswith("--from=")
            )
    return references


def compose_images(root, images, *, write=False):
    """Check every external CDC service; replace only managed image lines."""
    path = root / COMPOSE
    original = path.read_text()
    document = yaml.safe_load(original)
    services = document["services"]
    local_services = {"cdc-register", "cdc-consumer"}
    if any(
        services.get(name, {}).get("image")
        != "${COMPOSE_PROJECT_NAME:-lakehouse}-cdc:local"
        for name in local_services
    ):
        raise ImageError("CDC_LOCAL_IMAGE_CONTRACT_MISMATCH")
    external = {
        name: item.get("image")
        for name, item in services.items()
        if name not in local_services
    }
    if set(external) != set(SERVICES.values()):
        raise ImageError("CDC_EXTERNAL_IMAGE_COVERAGE_MISMATCH")
    rendered = original
    for name, service in SERVICES.items():
        # Keep human-written comments, anchors and interpolation unchanged.
        pattern = rf"(^  {service}:\n(?:(?!^  \S)[\s\S])*?^    image: )[^\n]+"
        rendered, count = re.subn(
            pattern,
            lambda match: match[1] + images[name],
            rendered,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise ImageError("CDC_COMPOSE_IMAGE_LINE_AMBIGUOUS")
        if not write and external[service] != images[name]:
            raise ImageError("CDC_COMPOSE_LOCK_MISMATCH: " + service)
    if write and rendered != original:
        path.write_text(rendered)


def failure_kind(message, returncode=1):
    """Classify explicit failures; cancellation never means a missing image."""
    message = message.lower()
    if returncode < 0 or re.search(r"interrupted|cancelled|canceled", message):
        return "COMMAND_INTERRUPTED"
    if "permission denied" in message or "cannot connect to the docker" in (
        message
    ):
        return "DOCKER_ACCESS_DENIED"
    if re.search(
        r"timeout|timed out|deadline exceeded|too many requests|"
        r"\b429\b|\b5\d\d\b|temporary failure|connection reset|"
        r"connection refused|unexpected eof|tls handshake",
        message,
    ):
        return "REGISTRY_TRANSIENT"
    if "no matching manifest" in message:
        return "PLATFORM_MISSING"
    if re.search(r"manifest unknown|no such manifest|: not found\b", message):
        return "MANIFEST_MISSING"
    if re.search(
        r"unauthorized|authentication required|denied|\b40[13]\b", (message)
    ):
        return "REGISTRY_AUTH_FAILED"
    return "REGISTRY_COMMAND_FAILED"


def docker(arguments, *, timeout=60):
    """Retry only transient errors with bounded deadlines and backoff.

    Buildx may itself retry requests. Keep an outer bound and never infer
    absence from timeout, unknown errors, a signal or access denial. All raw
    stderr remains private; evidence contains only public image references.
    """
    for attempt in range(ATTEMPTS):
        try:
            result = subprocess.run(
                ["docker", *arguments],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            kind = "REGISTRY_TRANSIENT"
        except OSError:
            raise ImageError("DOCKER_CLI_UNAVAILABLE") from None
        else:
            if result.returncode == 0:
                return result.stdout
            kind = failure_kind(
                result.stderr + "\n" + result.stdout, result.returncode
            )
        if kind != "REGISTRY_TRANSIENT" or attempt == ATTEMPTS - 1:
            raise ImageError(kind)
        time.sleep(2 ** (attempt + 1))
    raise AssertionError("unreachable")


def docker_json(arguments):
    """Require a valid JSON object rather than trusting CLI success alone."""
    try:
        result = json.loads(docker(arguments))
    except json.JSONDecodeError:
        raise ImageError("REGISTRY_RESPONSE_INVALID") from None
    if not isinstance(result, dict):
        raise ImageError("REGISTRY_RESPONSE_INVALID")
    return result


def inspect(reference, field="Manifest"):
    """Read registry descriptors or configs through buildx without a daemon."""
    return docker_json(
        [
            "buildx",
            "imagetools",
            "inspect",
            reference,
            "--format",
            "{{json ." + field + "}}",
        ]
    )


def validate_registry(reference):
    """Verify tag/pin equality, digest existence and the amd64 child/config."""
    pin = parse_pin(reference)
    repository, digest = pin["repository"], pin["digest"]
    tag = inspect(repository + ":" + pin["tag"])
    # Resolve the digest separately: a valid tag cannot prove a pin exists.
    pinned = inspect(repository + "@" + digest)
    if pinned.get("digest") != digest:
        raise ImageError("REGISTRY_PIN_DIGEST_MISMATCH")
    tag_digest = tag.get("digest", "")
    if not isinstance(tag_digest, str) or not re.fullmatch(DIGEST, tag_digest):
        raise ImageError("REGISTRY_TAG_DIGEST_INVALID")
    # A lock may name either the multiarch index or its amd64 manifest.
    # Python's shared base pin uses the latter; neither implies tag drift.
    platform_pin = (
        pinned.get("mediaType") in MANIFEST_TYPES
        and tag.get("mediaType") in INDEX_TYPES
        and any(
            item.get("digest") == digest
            and item.get("platform", {}).get("os") == "linux"
            and item.get("platform", {}).get("architecture") == "amd64"
            for item in tag.get("manifests", [])
        )
    )
    if tag_digest != digest and not platform_pin:
        raise ImageError("REGISTRY_TAG_DIGEST_MISMATCH")
    media_type = pinned.get("mediaType")
    if media_type in INDEX_TYPES:
        matches = [
            item
            for item in pinned.get("manifests", [])
            if item.get("platform", {}).get("os") == "linux"
            and item.get("platform", {}).get("architecture") == "amd64"
        ]
        if not matches:
            raise ImageError("PLATFORM_MISSING: linux/amd64")
        if len(matches) != 1:
            raise ImageError("PLATFORM_AMBIGUOUS: linux/amd64")
        platform_digest = matches[0].get("digest", "")
        if not re.fullmatch(DIGEST, platform_digest):
            raise ImageError("PLATFORM_DIGEST_INVALID")
        child = inspect(repository + "@" + platform_digest)
        if child.get("digest") != platform_digest:
            raise ImageError("PLATFORM_DIGEST_MISMATCH")
        if child.get("mediaType") not in MANIFEST_TYPES:
            raise ImageError("PLATFORM_MANIFEST_INVALID")
    elif media_type in MANIFEST_TYPES:
        platform_digest = digest
    else:
        raise ImageError("REGISTRY_MEDIA_TYPE_UNSUPPORTED")
    config = inspect(repository + "@" + platform_digest, "Image")
    if (config.get("os"), config.get("architecture")) != ("linux", "amd64"):
        raise ImageError("PLATFORM_CONFIG_MISMATCH")
    return {
        "image": reference,
        "media_type": media_type,
        "tag_digest": tag_digest,
        "tag_matches_pin": tag_digest == digest,
        "tag_contains_platform_pin": platform_pin,
        "pinned_digest": digest,
        "platform": "linux/amd64",
        "platform_digest": platform_digest,
        "registry": "passed",
        "pull": "not_requested",
    }


def canonical_repository(repository):
    """Normalize Docker Hub aliases before comparing Docker RepoDigests."""
    parts = repository.split("/")
    if "." not in parts[0] and ":" not in parts[0]:
        parts.insert(0, "docker.io")
    if parts[0] == "index.docker.io":
        parts[0] = "docker.io"
    if len(parts) == 2 and parts[0] == "docker.io":
        parts.insert(1, "library")
    return "/".join(parts)


def pull_smoke(evidence):
    """Pull immutable amd64 content and prove RepoDigest/config equality."""
    reference = evidence["image"]
    pin = parse_pin(reference)
    docker(["pull", "--platform", "linux/amd64", reference], timeout=300)
    image = docker_json(
        [
            "image",
            "inspect",
            reference,
            "--format",
            "{{json .}}",
        ]
    )
    if (image.get("Os"), image.get("Architecture")) != ("linux", "amd64"):
        raise ImageError("PULL_PLATFORM_MISMATCH")
    expected = canonical_repository(pin["repository"]) + "@" + pin["digest"]
    repo_digests = []
    for value in image.get("RepoDigests", []):
        repository, _, digest = value.partition("@")
        repo_digests.append(canonical_repository(repository) + "@" + digest)
    if expected not in repo_digests:
        raise ImageError("PULL_REPO_DIGEST_MISMATCH")
    evidence.update(pull="passed", repo_digest=expected)


def main(argv=None):
    """Check offline by default; registry/pull modes fail closed for CI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-compose",
        action="store_true",
        help="Generate managed CDC Compose image lines from images.lock.json.",
    )
    parser.add_argument(
        "--registry",
        action="store_true",
        help="Read registry tags, pins and linux/amd64 manifests/configs.",
    )
    parser.add_argument(
        "--pull-smoke",
        action="store_true",
        help="Also pull immutable linux/amd64 images; needs Docker access.",
    )
    arguments = parser.parse_args(argv)
    evidence = {"platform": "linux/amd64", "images": {}, "status": "failed"}
    current_image = None
    try:
        images = load_images(ROOT)
        compose_images(ROOT, images, write=arguments.write_compose)
        for name, reference in images.items():
            if arguments.registry or arguments.pull_smoke:
                print("Validating " + name, file=sys.stderr, flush=True)
                current_image = {
                    "name": name,
                    "image": reference,
                    "phase": "registry",
                }
                evidence["images"][name] = validate_registry(reference)
                if arguments.pull_smoke:
                    current_image["phase"] = "pull"
                    pull_smoke(evidence["images"][name])
        evidence["status"] = "passed"
    except (
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        OSError,
        yaml.YAMLError,
    ) as error:
        if current_image is not None:
            evidence["failed_image"] = current_image
        evidence["error"] = (
            str(error)
            if isinstance(error, ImageError)
            else "CDC_PUBLIC_SOURCE_INVALID"
        )
    print(json.dumps(evidence, indent=2))
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
