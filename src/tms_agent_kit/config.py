"""Workspace configuration and token loading."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import dotenv_values

CONFIG_FILE = "tms-agent-kit.toml"
DOTENV_FILE = ".env"
DEFAULT_ROOT = "cases"
DEFAULT_PLANS_ROOT = "plans"
DEFAULT_RUNS_ROOT = "runs"

# Environment variable that holds the API token, per TMS.
TOKEN_ENV = {"testit": "TESTIT_TOKEN"}

_KNOWN_KEYS = {"tms", "base_url", "project_id", "root", "plans_root", "runs_root"}
# Plain http is accepted only for local hosts: elsewhere it would expose the token.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ConfigError(Exception):
    """Invalid or missing configuration."""


@dataclass(frozen=True)
class Config:
    path: Path
    tms: str
    base_url: str
    project_id: str
    root: Path
    plans_root: Path
    runs_root: Path

    @property
    def workspace(self) -> Path:
        return self.path.parent


def find_config(start: Path) -> Path:
    """Return the nearest config file in `start` or its parents."""
    start = start.resolve()
    for directory in (start, *start.parents):
        candidate = directory / CONFIG_FILE
        if candidate.is_file():
            return candidate
    raise ConfigError(f"{CONFIG_FILE} not found in {start} or any parent directory")


def load_config(path: Path) -> Config:
    path = path.resolve()
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError as e:
        raise ConfigError(f"{path}: file not found") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e

    unknown = sorted(set(data) - _KNOWN_KEYS)
    if unknown:
        raise ConfigError(f"{path}: unknown keys: {', '.join(unknown)}")

    tms = _require_str(data, "tms", path)
    if tms not in TOKEN_ENV:
        supported = ", ".join(sorted(TOKEN_ENV))
        raise ConfigError(f"{path}: unsupported tms {tms!r}; supported: {supported}")

    roots = {
        key: _parse_root(key, data.get(key, default), path)
        for key, default in (
            ("root", DEFAULT_ROOT),
            ("plans_root", DEFAULT_PLANS_ROOT),
            ("runs_root", DEFAULT_RUNS_ROOT),
        )
    }
    # Each root holds one kind of file; nested roots would mix them.
    for a, b in (("root", "plans_root"), ("root", "runs_root"), ("plans_root", "runs_root")):
        if roots[a].is_relative_to(roots[b]) or roots[b].is_relative_to(roots[a]):
            raise ConfigError(f"{path}: {a} and {b} must not overlap")
    return Config(
        path=path,
        tms=tms,
        base_url=_parse_base_url(_require_str(data, "base_url", path), path),
        project_id=_parse_project_id(data.get("project_id"), path),
        root=roots["root"],
        plans_root=roots["plans_root"],
        runs_root=roots["runs_root"],
    )


def load_token(config: Config, environ: Mapping[str, str] | None = None) -> str:
    """Read the API token from the environment, then from the nearest `.env` that sets it."""
    environ = os.environ if environ is None else environ
    name = TOKEN_ENV[config.tms]
    token = environ.get(name)
    paths = dotenv_paths(config.workspace)
    for dotenv in paths:
        if token and token.strip():
            break
        if dotenv.is_file():
            token = dotenv_values(dotenv).get(name)
    token = (token or "").strip()
    if not token:
        where = paths[0] if len(paths) == 1 else f"{paths[0]} or {paths[-1]}"
        raise ConfigError(f"{name} is not set. Export it or add it to {where}.")
    return token


def dotenv_paths(workspace: Path) -> list[Path]:
    """`.env` files from the workspace up to the git repository root.

    Several projects in one repository can then share a `.env` at its root. Outside a git
    repository only the workspace is used, so a `.env` in an unrelated parent is never read.
    """
    chain = [workspace, *workspace.parents]
    for index, directory in enumerate(chain):
        if (directory / ".git").exists():
            return [d / DOTENV_FILE for d in chain[: index + 1]]
    return [workspace / DOTENV_FILE]


def _require_str(data: Mapping[str, Any], key: str, path: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path}: {key} is required and must be a non-empty string")
    return value.strip()


def _parse_base_url(value: str, path: Path) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ConfigError(f"{path}: base_url must be an absolute URL, e.g. https://host")
    if parts.query or parts.fragment:
        raise ConfigError(f"{path}: base_url must not contain a query or fragment")
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK_HOSTS:
        raise ConfigError(f"{path}: base_url must use https; http would send the token unencrypted")
    return value.rstrip("/")


def _parse_project_id(value: object, path: Path) -> str:
    # TOML users naturally write a global id as a number.
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ConfigError(f"{path}: project_id is required: a UUID or a positive global id")


def _parse_root(key: str, value: object, path: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path}: {key} must be a non-empty string")
    workspace = path.parent
    root = (workspace / value).resolve()
    # A root holds only its own files; the workspace also holds config and .tms state.
    if root == workspace or not root.is_relative_to(workspace):
        raise ConfigError(f"{path}: {key} must be a subdirectory of the workspace")
    return root
