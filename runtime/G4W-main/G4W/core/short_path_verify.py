import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path


def _content_hash(record: dict) -> str:
    return hashlib.sha256(str(record.get("content", "")).encode("utf-8")).hexdigest()


def verify_short_path_mirror(root: Path, expected_records) -> dict:
    """Compare logical expected records to mirror manifests by stable ID and body hash."""
    root = Path(root)
    expected = [dict(item) for item in expected_records]
    expected_ids = [str(item["stable_id"]) for item in expected]
    expected_hashes = {
        str(item["stable_id"]): str(item.get("content_sha256") or _content_hash(item))
        for item in expected
    }
    manifests = []
    invalid = []
    for path in sorted(root.rglob("*.json")) if root.exists() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            manifests.append((path, item))
        except (OSError, ValueError, TypeError) as error:
            invalid.append({"path": str(path), "error_type": type(error).__name__})

    mirror_ids = [str(item.get("stable_id", "")) for _, item in manifests]
    mirror_counts = Counter(mirror_ids)
    expected_counts = Counter(expected_ids)
    mirror_by_id = defaultdict(list)
    for path, item in manifests:
        mirror_by_id[str(item.get("stable_id", ""))].append((path, item))

    missing = sorted(set(expected_ids) - set(mirror_ids))
    extra = sorted(set(mirror_ids) - set(expected_ids))
    duplicates = sorted(key for key, count in mirror_counts.items() if key and count > 1)
    collisions = sorted(
        key for key, rows in mirror_by_id.items()
        if key and len({str(item.get("identity_sha256", "")) for _, item in rows}) > 1
    )
    hash_mismatches = []
    for stable_id in sorted(set(expected_ids) & set(mirror_ids)):
        actual = {str(item.get("content_sha256", "")) for _, item in mirror_by_id[stable_id]}
        body = {_content_hash(item) for _, item in mirror_by_id[stable_id]}
        wanted = expected_hashes[stable_id]
        if actual != {wanted} or body != {wanted}:
            hash_mismatches.append(stable_id)

    paths = [path.resolve() for path, _ in manifests]
    report = {
        "accepted_legacy_logical_count": len(expected),
        "expected_unique_count": len(expected_counts),
        "mirror_manifest_count": len(manifests),
        "mirror_unique_count": len(mirror_counts),
        "missing_ids": missing,
        "extra_ids": extra,
        "duplicate_ids": duplicates,
        "identity_collisions": collisions,
        "content_hash_mismatches": hash_mismatches,
        "invalid_manifests": invalid,
        "maximum_absolute_path_length": max((len(str(path)) for path in paths), default=0),
    }
    report["ok"] = not any((
        invalid, missing, extra, duplicates, collisions, hash_mismatches,
        len(expected) != len(expected_counts), len(expected_counts) != len(mirror_counts),
    ))
    return report
