"""List only portable skill sources; never capture task data or Git metadata."""
from hashlib import sha256
from pathlib import Path


def source_manifest(root):
    root = Path(root).resolve()
    patterns = ("SKILL.md", "README.md", "requirements*.txt", "references/*.md",
                "scripts/*.py", "scripts/unified/*.py", "scripts/fixtures/overlap-sanitized.json")
    paths = sorted({path for pattern in patterns for path in root.glob(pattern) if path.is_file()})
    records = []
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("skill_source_path_outside_root")
        data = path.read_bytes()
        records.append({"path": path.relative_to(root).as_posix(), "sha256": sha256(data).hexdigest(), "bytes": len(data)})
    return records
