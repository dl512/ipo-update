"""
Load environment variables from ``hkex/.env`` only (this file's directory).

Falls back to current working directory ``.env`` if ``hkex/.env`` is missing.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv


def hkex_dir() -> str:
    """Absolute path to the ``hkex`` package directory (folder containing this file)."""
    return os.path.dirname(os.path.abspath(__file__))


def load_hkex_dotenv() -> None:
    """Load ``<hkex>/.env`` with override=True."""
    env_path = os.path.join(hkex_dir(), ".env")
    if os.path.exists(env_path):
        load_dotenv(env_path, override=True)
        return
    if os.path.exists(".env"):
        load_dotenv(".env", override=True)
    else:
        load_dotenv(override=True)
