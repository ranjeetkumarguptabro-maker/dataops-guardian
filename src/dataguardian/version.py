
"""Version validation utilities for DataOps Guardian."""


def check_version(
    actual_version: str,
    required_version: str,
) -> dict:
    """Compare an actual version with the required version."""

    actual = actual_version.strip()
    required = required_version.strip()

    if not actual or not required:
        return {
            "name": "version_check",
            "status": "failed",
            "actual_version": actual,
            "required_version": required,
            "message": "Actual and required versions must not be empty",
        }

    passed = actual == required

    return {
        "name": "version_check",
        "status": "passed" if passed else "failed",
        "actual_version": actual,
        "required_version": required,
        "message": (
            "Version matches the required version"
            if passed
            else "Version mismatch"
        ),
    }
