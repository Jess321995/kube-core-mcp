from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from loguru import logger

class ServiceHandler(ABC):
    """Base class for service handlers"""

    @abstractmethod
    async def handle_command(self, command: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """Handle a command for this service"""
        pass

    @abstractmethod
    async def get_service_info(self) -> Dict[str, Any]:
        """Get information about the service"""
        pass

    @abstractmethod
    async def validate_command(self, command: str, parameters: Dict[str, Any]) -> bool:
        """Validate if the command can be handled by this service"""
        pass
