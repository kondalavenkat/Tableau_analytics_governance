"""
Work around a Windows temp-dir permissions issue in this environment.

Python's tempfile.mkdtemp() calls os.mkdir(path, 0o700). On Windows 3.13+
the mode is enforced, and in this sandbox that results in temp directories
that are not readable/writable by the current process. That breaks ensurepip,
venv creation, and pip installs.

We monkeypatch tempfile.mkdtemp() to create a unique directory using
os.makedirs() without applying restrictive ACL/mode changes.

This file is auto-imported by Python at startup (via the `site` module)
when it's discoverable on sys.path / PYTHONPATH.
"""

from __future__ import annotations

import os
import random
import string
import tempfile
from typing import Any


_orig_mkdtemp = tempfile.mkdtemp


def _mkdtemp_no_acl(suffix: str | None = None, prefix: str | None = None, dir: str | None = None) -> str:
    # Replicate tempfile defaults.
    if prefix is None:
        prefix = "tmp"
    if suffix is None:
        suffix = ""
    if dir is None:
        dir = tempfile.gettempdir()

    # Try a bounded number of candidates, similar to tempfile.TMP_MAX.
    for _ in range(10_000):
        name = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(8))
        path = os.path.join(dir, f"{prefix}{name}{suffix}")
        try:
            os.makedirs(path, exist_ok=False)
            return os.path.abspath(path)
        except FileExistsError:
            continue
    raise FileExistsError("No usable temporary directory name found (mkdtemp monkeypatch).")


def _patch_tempfile() -> None:
    tempfile.mkdtemp = _mkdtemp_no_acl  # type: ignore[assignment]

    # TemporaryDirectory uses mkdtemp under the hood, so this covers it too.


_patch_tempfile()

