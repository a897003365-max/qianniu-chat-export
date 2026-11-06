"""Recognition identity, distinct from immutable OCR artifact identity.

Only per-frame elapsed_seconds is telemetry. Preserve every other field,
including unknown fields, engine/profile identity, geometry and frame order.
"""
from collections import defaultdict
from copy import deepcopy
import hashlib
import json


def ocr_content_fingerprint(value):
    content = deepcopy(value)
    frames = content.get("frames")
    if isinstance(frames, list):
        for frame in frames:
            if isinstance(frame, dict):
                frame.pop("elapsed_seconds", None)
    data = json.dumps(content, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def group_ocr_versions(items):
    """Group verified items; retain all original source occurrences per group."""
    groups = defaultdict(list)
    for item in items:
        groups[ocr_content_fingerprint(item["value"])].append(item)
    return dict(sorted(groups.items()))
