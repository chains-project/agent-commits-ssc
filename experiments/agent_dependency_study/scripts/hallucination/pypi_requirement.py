"""Pure helpers for separating PEP 508 markers from registry constraints.

[IN]: A requirements version/direct-reference tail.
[OUT]: The same tail without a recognized environment-marker clause.
[POS]: Shared Stage1/Stage2 semantic boundary; no file or network access.
"""

from __future__ import annotations

import re


MARKER_START = re.compile(
    r";\s*(?=(?:python_version|python_full_version|os_name|sys_platform|"
    r"platform_release|platform_system|platform_version|platform_machine|"
    r"platform_python_implementation|implementation_name|"
    r"implementation_version|extra)\b)",
    re.IGNORECASE,
)


def without_environment_marker(value: str) -> str:
    """Return only the registry-relevant part of a PEP 508 requirement tail."""
    stripped = value.strip()
    marker = MARKER_START.search(stripped)
    return stripped[:marker.start()].rstrip() if marker else stripped
