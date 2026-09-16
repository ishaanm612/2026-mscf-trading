"""Small dependency-free loader for local RIT environment files."""
from __future__ import annotations

import os
import re
from pathlib import Path


_ENVIRONMENT_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")


def _value_without_comment(value: str) -> str:
    """Remove a comment from an unquoted dotenv value.

    :param value: Text following the first ``=`` in one dotenv record.
    :returns: A stripped unquoted value without an inline comment.
    """

    return value.split("#", maxsplit=1)[0].strip()


def _parse_value(value: str, line_number: int) -> str:
    """Parse a deliberately small, predictable subset of dotenv value syntax.

    Single- and double-quoted values preserve embedded ``#`` characters.  The
    loader intentionally does not expand shell variables or execute commands.

    :param value: Text following the first ``=`` in one dotenv record.
    :param line_number: Original one-based file line number for diagnostics.
    :returns: Decoded environment value.
    :raises ValueError: If a quoted value is not closed.
    """

    value = value.strip()
    if value[:1] not in {"'", '"'}:
        return _value_without_comment(value)
    quote = value[0]
    if len(value) < 2 or value[-1] != quote:
        raise ValueError(f"Unclosed quoted value on .env line {line_number}")
    return value[1:-1]


def load_env_file(path: str | Path, *, override: bool = False) -> tuple[str, ...]:
    """Load local key/value settings into ``os.environ`` without dependencies.

    Existing shell variables win by default so an operator can intentionally
    override a local file for one command.  The function returns names loaded,
    never values, which makes it safe to report configuration status.

    :param path: Location of a dotenv-format file.
    :param override: Replace existing environment values when ``True``.
    :returns: Names inserted or replaced in ``os.environ``; empty when absent.
    :raises ValueError: If a non-comment record is malformed.
    """

    file_path = Path(path)
    if not file_path.exists():
        return ()
    loaded: list[str] = []
    for line_number, original in enumerate(file_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        if "=" not in line:
            raise ValueError(f"Expected KEY=VALUE on .env line {line_number}")
        key, raw_value = line.split("=", maxsplit=1)
        key = key.strip()
        if not _ENVIRONMENT_KEY.fullmatch(key):
            raise ValueError(f"Invalid environment key on .env line {line_number}")
        if override or key not in os.environ:
            os.environ[key] = _parse_value(raw_value, line_number)
            loaded.append(key)
    return tuple(loaded)


def configure_case_environment(case: str) -> tuple[str, ...]:
    """Select optional case-specific RIT credentials for this process.

    ``RIT_ETF_*`` and ``RIT_VOLATILITY_*`` values take precedence over the
    legacy generic ``RIT_*`` variables once a runner has selected its case.
    This lets one ignored ``.env`` hold separate practice credentials without
    writing either set back to disk.  Generic variables remain a compatible
    fallback for REST and existing one-case configurations.

    :param case: ``etf`` or ``volatility``.
    :returns: Generic variable names populated from case-specific settings;
        values are intentionally never returned.
    :raises ValueError: If the case is unsupported.
    """

    prefixes = {"etf": "RIT_ETF", "volatility": "RIT_VOLATILITY"}
    try:
        prefix = prefixes[case]
    except KeyError as error:
        raise ValueError(f"Unsupported RIT case: {case}") from error
    selected: list[str] = []
    for suffix in ("API_MODE", "API_URL", "USERNAME", "PASSWORD", "API_KEY"):
        value = os.environ.get(f"{prefix}_{suffix}")
        if value is not None:
            os.environ[f"RIT_{suffix}"] = value
            selected.append(f"RIT_{suffix}")
    return tuple(selected)
