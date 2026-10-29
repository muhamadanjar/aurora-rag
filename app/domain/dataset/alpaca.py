from typing import Any


def validate_alpaca(content: Any) -> list[str]:
    """Validate the instruction/input/output record stored by an Alpaca example."""
    if not isinstance(content, dict):
        return ["content must be an object"]

    errors: list[str] = []
    unknown_fields = sorted(set(content) - {"instruction", "input", "output"})
    if unknown_fields:
        errors.append(f"content contains unsupported fields: {', '.join(unknown_fields)}")

    for field in ("instruction", "output"):
        value = content.get(field)
        if not isinstance(value, str):
            errors.append(f"{field} must be a string")
        elif not value.strip():
            errors.append(f"{field} must not be empty")

    if "input" in content and not isinstance(content["input"], str):
        errors.append("input must be a string")
    return errors
