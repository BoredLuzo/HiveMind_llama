def validate_name(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid")
    return value.strip()


def validate_city(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid")
    return value.strip()
