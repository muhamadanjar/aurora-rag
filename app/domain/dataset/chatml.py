from typing import Any


def validate_chatml(messages: list[dict[str, Any]]) -> list[str]:
    """Validate an alternating user/assistant ChatML transcript."""
    errors: list[str] = []
    if not messages:
        return ["messages must be a non-empty list"]
    if len(messages) < 2:
        errors.append("messages must contain at least two messages")
    if not isinstance(messages[-1], dict) or messages[-1].get("role") != "assistant":
        errors.append("messages must end with assistant")

    expected = "user"
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != expected:
            errors.append(f"message {index} must have role {expected}")
        if isinstance(message, dict):
            content = message.get("content")
            if not isinstance(content, str):
                errors.append(f"message {index} content must be a string")
            elif not content.strip():
                errors.append(f"message {index} content must not be empty")
        expected = "assistant" if expected == "user" else "user"
    return errors
