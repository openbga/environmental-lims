"""Read and compare the version 1 inventories emitted by verify_stored_files."""

import json
import re
from pathlib import Path

from django.core.management.base import CommandError


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def load_manifest_categories(path):
    try:
        manifest = json.loads(
            Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_keys
        )
    except OSError:
        raise CommandError("Could not read the saved file inventory.") from None
    except ValueError, RecursionError:
        raise CommandError("The saved file inventory is not valid JSON with unique keys.") from None

    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"version", "categories"}
        or type(manifest["version"]) is not int
        or manifest["version"] != 1
        or not isinstance(manifest["categories"], dict)
    ):
        raise CommandError("Expected a version 1 stored-file inventory.")

    for entry in manifest["categories"].values():
        if (
            not isinstance(entry, dict)
            or set(entry) != {"count", "sha256"}
            or type(entry["count"]) is not int
            or entry["count"] < 0
            or not isinstance(entry["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        ):
            raise CommandError("Invalid category count or SHA-256 digest in the saved inventory.")
    return manifest["categories"]


def inventory_differences(saved, restored):
    # Only print category names supplied by the running application.
    for category, actual in restored.items():
        if category not in saved:
            yield f"{category}: missing from saved inventory."
        elif saved[category]["count"] != actual["count"]:
            yield (
                f"{category}: saved count {saved[category]['count']}, "
                f"restored count {actual['count']}."
            )
        elif saved[category]["sha256"] != actual["sha256"]:
            yield f"{category}: SHA-256 digest differs at the same count ({actual['count']})."
    if saved.keys() - restored.keys():
        yield "The saved inventory contains categories absent from this application version."
