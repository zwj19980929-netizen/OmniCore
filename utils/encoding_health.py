"""
Repository encoding health checker.

Detects common mojibake signals and invalid Unicode code points that
should not appear in source files.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence


DEFAULT_SOURCE_EXTENSIONS = {
    ".py",
    ".md",
    ".txt",
    ".yaml",
    ".yml",
    ".toml",
    ".json",
    ".ini",
}

DEFAULT_EXCLUDE_DIRS = {
    ".git",
    ".idea",
    ".pytest_cache",
    "__pycache__",
    "data",
    "venv",
    ".venv",
    "model",
}

DEFAULT_EXCLUDE_FILES = {
    "utils/encoding_health.py",
    "error.txt",
}

IGNORE_LINE_MARKER = "encoding-health: ignore-line"
IGNORE_START_MARKER = "encoding-health: ignore-start"
IGNORE_END_MARKER = "encoding-health: ignore-end"

# Common mojibake fragments observed in UTF-8/GBK mis-decoding cases.
MOJIBAKE_TOKENS = (
    "锛",
    "銆",
    "鈥",
    "鍙",
    "闂",
    "璇",
    "缁",
    "绯",
    "鎿",
    "浣",
    "妯",
    "瀵",
    "鎰",
    "瑙",
    "鍛",
)


@dataclass(frozen=True)
class EncodingIssue:
    file_path: Path
    line: int
    column: int
    kind: str
    token: str
    snippet: str

    def render(self, root: Path) -> str:
        try:
            rel_path = self.file_path.resolve().relative_to(root.resolve())
        except ValueError:
            rel_path = self.file_path
        return (
            f"{rel_path}:{self.line}:{self.column} "
            f"[{self.kind}] token={self.token!r} snippet={self.snippet!r}"
        )


def _iter_files(
    root: Path,
    *,
    extensions: Sequence[str],
    exclude_dirs: Sequence[str],
    exclude_files: Sequence[str],
) -> Iterable[Path]:
    if not root.exists():
        return

    ext_set = {item.lower() for item in extensions}
    excluded = {item.lower() for item in exclude_dirs}
    excluded_files = {
        item.replace("\\", "/").lower().lstrip("./")
        for item in exclude_files
    }

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part.lower() in excluded for part in path.parts):
            continue
        rel_norm = path.relative_to(root).as_posix().lower().lstrip("./")
        if rel_norm in excluded_files:
            continue
        if path.suffix.lower() not in ext_set:
            continue
        yield path


def scan_file(file_path: Path) -> List[EncodingIssue]:
    issues: List[EncodingIssue] = []
    try:
        content = file_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        issues.append(
            EncodingIssue(
                file_path=file_path,
                line=max(getattr(exc, "start", 0), 0) + 1,
                column=1,
                kind="decode_error",
                token="utf-8-sig",
                snippet=str(exc),
            )
        )
        return issues

    ignore_block = False
    for line_no, line in enumerate(content.splitlines(), 1):
        if IGNORE_START_MARKER in line:
            ignore_block = True
            continue
        if IGNORE_END_MARKER in line:
            ignore_block = False
            continue
        if ignore_block or IGNORE_LINE_MARKER in line:
            continue

        for col_no, ch in enumerate(line, 1):
            code = ord(ch)
            if 0xE000 <= code <= 0xF8FF:
                issues.append(
                    EncodingIssue(
                        file_path=file_path,
                        line=line_no,
                        column=col_no,
                        kind="private_use_char",
                        token=f"U+{code:04X}",
                        snippet=line.strip()[:200],
                    )
                )
            elif code == 0xFFFD:
                issues.append(
                    EncodingIssue(
                        file_path=file_path,
                        line=line_no,
                        column=col_no,
                        kind="replacement_char",
                        token="U+FFFD",
                        snippet=line.strip()[:200],
                    )
                )

        for token in MOJIBAKE_TOKENS:
            index = line.find(token)
            if index < 0:
                continue
            issues.append(
                EncodingIssue(
                    file_path=file_path,
                    line=line_no,
                    column=index + 1,
                    kind="mojibake_token",
                    token=token,
                    snippet=line.strip()[:200],
                )
            )
    return issues


def scan_project(
    root: Path,
    *,
    extensions: Sequence[str] = tuple(DEFAULT_SOURCE_EXTENSIONS),
    exclude_dirs: Sequence[str] = tuple(DEFAULT_EXCLUDE_DIRS),
    exclude_files: Sequence[str] = tuple(DEFAULT_EXCLUDE_FILES),
) -> List[EncodingIssue]:
    all_issues: List[EncodingIssue] = []
    for file_path in _iter_files(
        root,
        extensions=extensions,
        exclude_dirs=exclude_dirs,
        exclude_files=exclude_files,
    ):
        all_issues.extend(scan_file(file_path))
    return all_issues


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Check repository encoding health.")
    parser.add_argument(
        "--root",
        default=".",
        help="Project root directory (default: current directory).",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    root = Path(args.root).resolve()
    issues = scan_project(root)
    if not issues:
        print("Encoding health check passed.")
        return 0

    print(f"Encoding health check failed: {len(issues)} issue(s).")
    for issue in issues:
        print(issue.render(root))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
