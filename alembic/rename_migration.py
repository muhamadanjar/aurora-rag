"""Post-write hook enforcing sequential Alembic migration filenames."""

import os
import re
import sys


def rename_migration(filepath: str) -> None:
    """Rename a hash-based revision and synchronize its revision ID."""
    versions_dir = os.path.dirname(filepath)
    filename = os.path.basename(filepath)
    if re.match(r"^\d{4}_", filename):
        return

    match = re.match(r"^[a-f0-9]+_(.+)\.py$", filename)
    if not match:
        return

    numbers = []
    for existing in os.listdir(versions_dir):
        number_match = re.match(r"^(\d+)_", existing)
        if number_match:
            numbers.append(int(number_match.group(1)))

    revision = f"{(max(numbers) if numbers else 0) + 1:04d}"
    new_filename = f"{revision}_{match.group(1)}.py"
    new_filepath = os.path.join(versions_dir, new_filename)

    with open(filepath, encoding="utf-8") as migration_file:
        content = migration_file.read()
    content = re.sub(
        r"(revision\s*=\s*[\"'])([^\"']+)([\"'])",
        rf"\g<1>{revision}\g<3>",
        content,
        count=1,
    )

    with open(new_filepath, "w", encoding="utf-8") as migration_file:
        migration_file.write(content)
    with open(filepath, "w", encoding="utf-8") as migration_file:
        migration_file.write(content)


if __name__ == "__main__" and len(sys.argv) > 1:
    rename_migration(sys.argv[1])
