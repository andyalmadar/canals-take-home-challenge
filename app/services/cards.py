from __future__ import annotations


def luhn_valid(digits: str) -> bool:
    """Luhn checksum (ISO/IEC 7812-1). `digits` must be a non-empty numeric string."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        digit = int(char)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0
