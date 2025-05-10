from kubernetes import client, config
from loguru import logger
from typing import Dict, Any, List, Optional, Tuple
import os
import re
import subprocess
from service_handler import ServiceHandler
from dataclasses import dataclass
from enum import Enum

class SecurityMode(Enum):
    """Security modes for command validation"""
    STRICT = "strict"  # Only allow specific commands and parameters
    PERMISSIVE = "permissive"  # Allow any kubectl command with basic safety checks

@dataclass
class CommandResult:
    """Result of a command execution"""
    success: bool
    output: str
    error: Optional[str] = None
    exit_code: int = 0

class KubernetesHandler(ServiceHandler):
    def __init__(self, security_mode: SecurityMode = SecurityMode.STRICT):
        """Initialize Kubernetes client and security settings"""
        try:
            # Try to load in-cluster config first
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes configuration")
        except config.ConfigException:
            # Fall back to kubeconfig
            config.load_kube_config()
            logger.info("Loaded kubeconfig")

        self.v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()
        self.batch_v1 = client.BatchV1Api()
        self.security_mode = security_mode

        # Define allowed commands and their patterns
        self.allowed_commands = {
            "kubectl": {
                # Basic structure: kubectl <command> <resource-type>/<name> [any flags]
                "get": r"^kubectl\s+get\s+(pods?|deployments?|services?|namespaces?|configmaps?|secrets?)(?:\s+[^\s]+)*$",
                "describe": r"^kubectl\s+describe\s+(pod|deployment|service|namespace|configmap|secret)\s+\w+(?:\s+[^\s]+)*$",
                "create": r"^kubectl\s+create\s+(deployment|namespace|service)\s+\w+(?:\s+[^\s]+)*$",
                "delete": r"^kubectl\s+delete\s+(pod|deployment|service|namespace)\s+\w+(?:\s+[^\s]+)*$",
                # Completely permissive logs pattern that allows any flags in any order
                "logs": r"^kubectl\s+logs(?:\s+[^\s]+)*$",
                "scale": r"^kubectl\s+scale\s+deployment\s+\w+(?:\s+[^\s]+)*$",
                "exec": r"^kubectl\s+exec\s+(?:\s+[^\s]+)*\s+--\s+\w+.*$",
                "config": r"^kubectl\s+config\s+(use-context|get-contexts|current-context)(?:\s+[^\s]+)*$"
            },
            "helm": {
                # Keep helm commands as is since they're less frequently used
                "list": r"^helm\s+list(\s+--all-namespaces|\s+-n\s+\w+)?$",
                "install": r"^helm\s+install\s+\w+\s+\S+(\s+--namespace\s+\w+|\s+--set\s+\S+)*$",
                "uninstall": r"^helm\s+uninstall\s+\w+(\s+--namespace\s+\w+)?$",
                "upgrade": r"^helm\s+upgrade\s+\w+\s+\S+(\s+--namespace\s+\w+|\s+--set\s+\S+)*$"
            }
        }

        # Define forbidden patterns (regardless of security mode)
        self.forbidden_patterns = [
            # General dangerous patterns
            r".*--privileged.*",
            r".*--host-network.*",
            r".*--host-pid.*",
            r".*--host-ipc.*",
            r".*--as=root.*",
            r".*--as=system:admin.*",
            r".*delete\s+namespace\s+kube-system.*",
            r".*delete\s+namespace\s+default.*",

            # Dangerous API access patterns
            r".*--raw.*",
            r".*--v=[4-9].*",
            r".*--insecure-skip-tls-verify.*",
            r".*--token=.*",
            r".*--client-certificate=.*",
            r".*--client-key=.*",

            # Additional dangerous patterns
            r".*--force.*",  # Prevents force deletion
            r".*--grace-period=0.*",  # Prevents immediate deletion
            r".*--now.*",  # Prevents immediate deletion
            r".*--cascade=orphan.*",  # Prevents orphaned resources
            r".*--all.*",  # Prevents mass deletion
            r".*--selector=.*",  # Prevents mass deletion by selector
            r".*--field-selector=.*",  # Prevents mass deletion by field selector
            r".*--all-namespaces.*delete.*",  # Prevents mass deletion across namespaces
            r".*--dry-run=server.*",  # Prevents server-side dry run
            r".*--server-side.*",  # Prevents server-side apply
            r".*--force-conflicts.*",  # Prevents force conflicts
            r".*--validate=false.*"  # Prevents skipping validation
        ]

    def _validate_command(self, command: str) -> Tuple[bool, Optional[str]]:
        """Validate if a command is allowed based on security settings"""
        logger.info(f"Validating command: {command}")

        # Check forbidden patterns first
        for pattern in self.forbidden_patterns:
            if re.match(pattern, command, re.IGNORECASE):
                logger.warning(f"Command matches forbidden pattern: {pattern}")
                return False, f"Command matches forbidden pattern: {pattern}"

        if self.security_mode == SecurityMode.PERMISSIVE:
            # In permissive mode, only check for forbidden patterns
            return True, None

        # In strict mode, validate against allowed commands
        parts = command.split()
        if not parts:
            logger.warning("Empty command")
            return False, "Empty command"

        tool = parts[0]
        if tool not in self.allowed_commands:
            logger.warning(f"Tool {tool} not allowed. Allowed tools: {list(self.allowed_commands.keys())}")
            return False, f"Tool {tool} not allowed"

        if len(parts) < 2:
            logger.warning(f"Command too short: {command}")
            return False, "Command too short"

        subcommand = parts[1]
        if subcommand not in self.allowed_commands[tool]:
            logger.warning(f"Subcommand {subcommand} not allowed for {tool}. Allowed subcommands: {list(self.allowed_commands[tool].keys())}")
            return False, f"Subcommand {subcommand} not allowed for {tool}"

        pattern = self.allowed_commands[tool][subcommand]
        logger.info(f"Checking command against pattern: {pattern}")

        if not re.match(pattern, command, re.IGNORECASE):
            logger.warning(f"Command '{command}' does not match pattern: {pattern}")
            # Try to explain why it didn't match
            if subcommand == "get":
                if not any(resource in command for resource in ["pod", "deployment", "service", "namespace", "configmap", "secret"]):
                    return False, "Command must specify a valid resource type (pod, deployment, service, namespace, configmap, or secret)"
                if not any(flag in command for flag in ["--all-namespaces", "-n"]):
                    return False, "Command must include either --all-namespaces or -n <namespace>"
            return False, f"Command does not match allowed pattern: {pattern}"

        logger.info(f"Command '{command}' is valid")
        return True, None

    async def execute_command(self, command: str) -> CommandResult:
        """Execute a validated kubectl or helm command"""
        try:
            # Validate command
            is_valid, error = self._validate_command(command)
            if not is_valid:
                logger.error(f"Command validation failed: {error}")
                return CommandResult(success=False, output="", error=error, exit_code=1)

            # Execute command
            logger.info(f"Executing command: {command}")
            process = subprocess.Popen(
                command.split(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            stdout, stderr = process.communicate()

            if process.returncode != 0:
                logger.error(f"Command failed with error: {stderr}")
            else:
                logger.info("Command executed successfully")

            return CommandResult(
                success=process.returncode == 0,
                output=stdout,
                error=stderr if process.returncode != 0 else None,
                exit_code=process.returncode
            )

        except Exception as e:
            logger.error(f"Error executing command '{command}': {str(e)}")
            return CommandResult(
                success=False,
                output="",
                error=str(e),
                exit_code=1
            )

    async def get_service_info(self) -> Dict[str, Any]:
        """Get information about the Kubernetes service"""
        try:
            version = self.v1.get_api_resources()
            return {
                "name": "kubernetes",
                "version": version,
                "security_mode": self.security_mode.value,
                "capabilities": {
                    "kubectl": list(self.allowed_commands["kubectl"].keys()),
                    "helm": list(self.allowed_commands["helm"].keys())
                }
            }
        except Exception as e:
            logger.error(f"Error getting Kubernetes service info: {str(e)}")
            raise

    async def handle_command(self, command: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """Handle a command by executing it directly"""
        try:
            # The command should be a pre-formatted kubectl or helm command
            # Parameters are ignored as the command should be complete
            result = await self.execute_command(command)

            if not result.success:
                raise ValueError(f"Command failed: {result.error}")

            return {
                "status": "success",
                "output": result.output,
                "command": command
            }

        except Exception as e:
            logger.error(f"Error handling command {command}: {str(e)}")
            raise

    async def validate_command(self, command: str, parameters: Dict[str, Any]) -> bool:
        """Validate if a command can be handled by this service"""
        try:
            # For Kubernetes handler, we only validate the command string
            # Parameters are ignored as they are part of the command string
            is_valid, _ = self._validate_command(command)
            return is_valid
        except Exception as e:
            logger.error(f"Error validating command: {str(e)}")
            return False
