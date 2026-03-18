"""
tests/test_student_registry.py — Package-list parity guard.

Ensures that student_registry.AVAILABLE_STUDENT_PACKAGES never drifts
out of sync with relay/api/_registry.AVAILABLE_STUDENT_PACKAGES.
If both lists are not identical (regardless of order), students who
subscribe to a package via the relay will be silently skipped by
student_digest.py's batch loop.
"""

from __future__ import annotations

import student_registry
from relay.api import _registry as relay_registry


def test_package_lists_match() -> None:
    """Root registry and relay registry must expose the same package IDs."""
    root = sorted(student_registry.AVAILABLE_STUDENT_PACKAGES)
    relay = sorted(relay_registry.AVAILABLE_STUDENT_PACKAGES)
    assert root == relay, (
        f"Package list mismatch between student_registry.py and relay/api/_registry.py.\n"
        f"  student_registry: {root}\n"
        f"  relay/_registry:  {relay}\n"
        "Update the shorter list to include all packages from the longer one."
    )
