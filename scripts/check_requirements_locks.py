from __future__ import annotations

from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _normalize(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def _locked_versions(path: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "==" not in stripped:
            continue
        name, version = stripped.split("==", 1)
        versions[_normalize(name)] = version.split(";", 1)[0].strip()
    return versions


def _declared_requirements(path: Path) -> list[Requirement]:
    requirements: list[Requirement] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-")):
            continue
        # Drop trailing inline comments (e.g. "pycron>=3.0.0  # note") so the
        # remainder parses as a PEP 508 requirement.
        stripped = stripped.split("#", 1)[0].strip()
        if not stripped:
            continue
        requirements.append(Requirement(stripped))
    return requirements


def _assert_locked_satisfies(declared: Path, locked: dict[str, str], lock_name: str) -> None:
    """Every direct requirement must be pinned in the lock at a version that
    satisfies the declared floor. Name-presence alone is not enough: a bumped
    floor with a stale lock would otherwise pass silently while the old version
    stays installed."""
    for requirement in _declared_requirements(declared):
        pinned = locked.get(_normalize(requirement.name))
        if pinned is None:
            raise SystemExit(
                f"{lock_name} is missing a direct requirement declared in {declared.name}: {requirement.name}"
            )
        # prereleases=True so beta pins (e.g. OpenTelemetry 0.62b1) satisfy a
        # beta floor (>=0.48b0) instead of being rejected as pre-releases.
        if requirement.specifier and not requirement.specifier.contains(Version(pinned), prereleases=True):
            raise SystemExit(
                f"{lock_name} pins {requirement.name}=={pinned}, which does not satisfy the "
                f"floor '{requirement.specifier}' declared in {declared.name}; regenerate the lock"
            )


def _assert_contains(path: Path, expected: str) -> None:
    text = path.read_text()
    if expected not in text:
        raise SystemExit(f"{path.relative_to(PROJECT_ROOT)} must contain: {expected}")


def main() -> None:
    runtime = _locked_versions(PROJECT_ROOT / "requirements.lock")
    dev = _locked_versions(PROJECT_ROOT / "requirements-dev.lock")
    conflicts = {
        name: (runtime[name], dev[name]) for name in sorted(runtime.keys() & dev.keys()) if runtime[name] != dev[name]
    }
    if conflicts:
        raise SystemExit(f"runtime/dev lock version conflicts: {conflicts}")

    _assert_locked_satisfies(PROJECT_ROOT / "requirements.txt", runtime, "requirements.lock")
    _assert_locked_satisfies(PROJECT_ROOT / "requirements-dev.txt", dev, "requirements-dev.lock")

    _assert_contains(PROJECT_ROOT / "Dockerfile", "pip install --no-cache-dir -r requirements.lock")
    _assert_contains(
        PROJECT_ROOT / ".github/workflows/ci.yml", "pip install -r requirements.lock -r requirements-dev.lock"
    )
    _assert_contains(
        PROJECT_ROOT / ".github/workflows/ci.yml", "pip-audit -r requirements.lock -r requirements-dev.lock"
    )

    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text()
    has_project_metadata = "\n[project]\n" in f"\n{pyproject}\n"
    if (PROJECT_ROOT / "uv.lock").exists() and not has_project_metadata:
        raise SystemExit("uv.lock exists without [project] metadata; use requirements.lock files instead")


if __name__ == "__main__":
    main()
