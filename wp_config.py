"""
Shared WordPress connection settings, loaded from .env.

Every script that talks to the site imports from here, so the hostname and
credentials exist in exactly one place -- .env, which is gitignored. Nothing in
the tracked source names the site, which keeps the staging URL out of the repo.

Expects, in a .env file at the project root:

    WP_BASE_URL=https://<host>/wp-json/wp/v2
    WP_USER=<username>
    WP_KEY=<application password>
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

# Find .env from the project root rather than the caller's working directory,
# so scripts in misc/ and pipeline/ behave the same as ones at the top level.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


def _require(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        sys.exit(
            f"error: {name} is not set.\n"
            f"       Create a .env file at the project root with WP_BASE_URL, "
            f"WP_USER and WP_KEY.\n"
            f"       See wp_config.py for the expected format."
        )
    return value


def base_url() -> str:
    """The REST API root, e.g. https://<host>/wp-json/wp/v2 (no trailing slash)."""
    return _require("WP_BASE_URL").rstrip("/")


def auth() -> HTTPBasicAuth:
    """HTTP basic auth from the configured user and application password."""
    return HTTPBasicAuth(_require("WP_USER"), _require("WP_KEY"))
