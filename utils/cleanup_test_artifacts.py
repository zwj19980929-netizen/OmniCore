"""
Cleanup utility for transient test artifacts under the project data directory.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence


DEFAULT_EXTRA_TARGETS = ("browser_state_smoketest",)
TEST_PREFIXES = ("test_", "test-")


@dataclass(frozen=True)
class CleanupResult:
    path: Path
    removed: bool
    reason: str


def discover_cleanup_targets(
    project_root: Path,
    *,
    include_debug: bool = False,
    include_logs: bool = False,
) -> List[Path]:
    data_dir = project_root / "data"
    if not data_dir.exists():
        return []

    targets: List[Path] = []
    seen: set[Path] = set()

    for child in data_dir.iterdir():
        name = child.name
        should_remove = name in DEFAULT_EXTRA_TARGETS or name.startswith(TEST_PREFIXES)
        if not should_remove:
            continue
        resolved = child.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        targets.append(child)

    if include_debug:
        debug_dir = data_dir / "debug"
        if debug_dir.exists() and debug_dir.resolve() not in seen:
            seen.add(debug_dir.resolve())
            targets.append(debug_dir)

    if include_logs:
        logs_dir = data_dir / "logs"
        if logs_dir.exists() and logs_dir.resolve() not in seen:
            seen.add(logs_dir.resolve())
            targets.append(logs_dir)

    return sorted(targets, key=lambda item: item.as_posix())


def remove_targets(targets: Sequence[Path], *, dry_run: bool = False) -> List[CleanupResult]:
    results: List[CleanupResult] = []
    for target in targets:
        if not target.exists():
            results.append(CleanupResult(path=target, removed=False, reason="missing"))
            continue
        if dry_run:
            results.append(CleanupResult(path=target, removed=False, reason="dry-run"))
            continue

        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
        results.append(CleanupResult(path=target, removed=True, reason="removed"))
    return results


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Remove transient test artifacts under the project's data directory.",
    )
    parser.add_argument(
        "--project-root",
        default=Path.cwd(),
        type=Path,
        help="Project root containing the data directory. Defaults to the current working directory.",
    )
    parser.add_argument(
        "--include-debug",
        action="store_true",
        help="Also remove data/debug.",
    )
    parser.add_argument(
        "--include-logs",
        action="store_true",
        help="Also remove data/logs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the targets without deleting them.",
    )
    return parser


def _render_results(results: Iterable[CleanupResult], project_root: Path) -> str:
    lines = []
    for item in results:
        try:
            label = item.path.resolve().relative_to(project_root.resolve()).as_posix()
        except ValueError:
            label = item.path.resolve().as_posix()
        lines.append(f"{item.reason:8} {label}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    project_root = Path(args.project_root).resolve()

    targets = discover_cleanup_targets(
        project_root,
        include_debug=bool(args.include_debug),
        include_logs=bool(args.include_logs),
    )
    if not targets:
        print("No transient test artifacts found.")
        return 0

    results = remove_targets(targets, dry_run=bool(args.dry_run))
    print(_render_results(results, project_root))
    removed_count = sum(1 for item in results if item.removed)
    if args.dry_run:
        print(f"Dry run complete. {len(results)} target(s) matched.")
    else:
        print(f"Removed {removed_count} target(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
