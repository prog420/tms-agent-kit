"""YAML frontmatter shared by case, plan and run files."""

from __future__ import annotations

import re
from typing import Any

import yaml


class FormatError(ValueError):
    """A file does not follow its format. Carries the 1-based line number when known."""

    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(f"line {line}: {message}" if line else message)
        self.line = line


# YAML loader that keeps ISO dates as strings: the frontmatter round-trips them verbatim.
class _Loader(yaml.SafeLoader):
    pass


_Loader.yaml_implicit_resolvers = {
    key: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def split_frontmatter(lines: list[str]) -> tuple[list[str], int]:
    """Frontmatter lines and the index of the first body line. Frontmatter is optional."""
    if not lines or lines[0].strip() != "---":
        return [], 0
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return lines[1:index], index + 1
    raise FormatError("frontmatter is not closed with '---'", 1)


def dump_yaml(data: dict[str, Any]) -> str:
    """Block style at the top level, flow style for nested lists and mappings."""
    return "".join(
        yaml.dump(
            {key: value},
            Dumper=yaml.SafeDumper,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=None if isinstance(value, (list, dict)) else False,
            width=1000,
        )
        for key, value in data.items()
    )


class Frontmatter:
    """Parsed frontmatter mapping with file line numbers for error messages."""

    FIRST_LINE = 2  # line 1 is the opening '---'

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        try:
            data = yaml.load("\n".join(lines), Loader=_Loader) or {}  # noqa: S506 - safe loader subclass
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            line = mark.line + self.FIRST_LINE if mark is not None else None
            problem = getattr(e, "problem", None) or str(e)
            raise FormatError(f"invalid frontmatter: {problem}", line) from e
        if not isinstance(data, dict):
            raise FormatError("frontmatter must be a mapping", self.FIRST_LINE)
        self.data: dict[str, Any] = data

    def line(self, key: str) -> int | None:
        pattern = re.compile(rf"^{re.escape(key)}\s*:")
        for index, text in enumerate(self.lines):
            if pattern.match(text):
                return index + self.FIRST_LINE
        return None

    def error(self, key: str, message: str) -> FormatError:
        return FormatError(message, self.line(key))

    def optional(self, key: str, kind: type) -> Any:
        value = self.data.get(key)
        if value is None:
            return None
        # bool is a subclass of int; do not accept true/false as a number.
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise self.error(key, f"{key} must be of type {kind.__name__}, got {value!r}")
        return value

    def choice(self, key: str, allowed: tuple[str, ...]) -> str | None:
        """An enum value, matched case-insensitively and returned in canonical form."""
        value = self.optional(key, str)
        if value is None:
            return None
        canonical = {a.casefold(): a for a in allowed}.get(value.strip().casefold())
        if canonical is None:
            raise self.error(key, f"{key} must be one of: {', '.join(allowed)}; got {value!r}")
        return canonical

    def string_list(self, key: str) -> tuple[str, ...] | None:
        value = self.data.get(key)
        if value is None:
            return None
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise self.error(key, f"{key} must be a list of non-empty strings")
        return tuple(v.strip() for v in value)

    def check_keys(self, allowed: set[str], hint: str) -> None:
        unknown = sorted(set(self.data) - allowed)
        if unknown:
            raise self.error(unknown[0], f"unknown frontmatter keys: {', '.join(unknown)}; {hint}")
