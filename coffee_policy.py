"""Fixed creator support banner policy. URL is never configurable by users."""
from __future__ import annotations
from dataclasses import dataclass

DEFAULT_COFFEE_URL = "https://buymeacoffee.com/nipu011"


def validated_coffee_url(value: str) -> str:
    """Reject any destination other than the app's built-in creator profile."""
    if value != DEFAULT_COFFEE_URL:
        raise ValueError("The support destination is fixed in IrisPD")
    return DEFAULT_COFFEE_URL


@dataclass(frozen=True)
class CoffeeSettings:
    enabled: bool = True
    duration_seconds: int = 60

    @property
    def url(self) -> str:
        """Read-only constant for compatibility with the previous version."""
        return DEFAULT_COFFEE_URL

    def validate(self) -> None:
        if type(self.enabled) is not bool or self.duration_seconds != 60:
            raise ValueError("Support banner options are invalid")
