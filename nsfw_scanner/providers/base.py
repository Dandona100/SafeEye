"""Abstract base class for scan providers."""
from abc import ABC, abstractmethod
from nsfw_scanner.models import ProviderResult


class BaseProvider(ABC):
    name: str = "unknown"

    @abstractmethod
    async def scan(self, file_path: str) -> ProviderResult:
        """Scan a file and return a ProviderResult."""

    def is_configured(self) -> bool:
        """Check if this provider has the required dependencies/credentials."""
        return True
