from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _locked_versions(path: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "==" not in stripped:
            continue
        name, version = stripped.split("==", 1)
        normalized = name.lower().replace("_", "-").replace(".", "-")
        versions[normalized] = version.split(";", 1)[0].strip()
    return versions


def _declared_package_names(path: Path) -> set[str]:
    names: set[str] = set()
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-")):
            continue
        name = stripped.split(";", 1)[0]
        for sep in ("[", "==", ">=", "<=", "~=", "!=", ">", "<"):
            name = name.split(sep, 1)[0]
        normalized = name.strip().lower().replace("_", "-").replace(".", "-")
        if normalized:
            names.add(normalized)
    return names


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

    runtime_declared = _declared_package_names(PROJECT_ROOT / "requirements.txt")
    runtime_missing = runtime_declared - set(runtime)
    if runtime_missing:
        raise SystemExit(f"requirements.lock is missing direct runtime requirements: {sorted(runtime_missing)}")

    dev_declared = _declared_package_names(PROJECT_ROOT / "requirements-dev.txt")
    dev_missing = dev_declared - set(dev)
    if dev_missing:
        raise SystemExit(f"requirements-dev.lock is missing direct dev requirements: {sorted(dev_missing)}")

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
