def authorize(subject: str, owner: str) -> bool:
    return bool(subject) and subject == owner
