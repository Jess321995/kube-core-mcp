from kubernetes import client, config
from loguru import logger
from typing import Dict, Any, List, Optional, Tuple
import os
import re
import subprocess
from service_handler import ServiceHandler
from dataclasses import dataclass
from enum import Enum
import asyncio
import json

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
    analysis: Optional[Dict[str, Any]] = None

class KubernetesHandler(ServiceHandler):
    def __init__(self, security_mode: SecurityMode = SecurityMode.PERMISSIVE):
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
                "get": r"^kubectl\s+get\s+(pods?|deployments?|services?|namespaces?|configmaps?|secrets?|nodes?)(?:\s+[^\s]+)*$",
                "describe": r"^kubectl\s+describe\s+(pod|deployment|service|namespace|configmap|secret|node)\s+[^\s]+(?:\s+[^\s]+)*$",
                "create": r"^kubectl\s+create(?:\s+[^\s]+)*\s+(deployment|namespace|service)(?:\s+[^\s]+)*$",
                "delete": r"^kubectl\s+delete\s+(pod|deployment|service|namespace)\s+\w+(?:\s+[^\s]+)*$",
                "logs": r"^kubectl\s+logs(?:\s+[^\s]+)*$",
                "scale": r"^kubectl\s+scale\s+deployment\s+\w+(?:\s+[^\s]+)*$",
                "exec": r"^kubectl\s+exec\s+(?:\s+[^\s]+)*\s+--\s+\w+.*$",
                "config": r"^kubectl\s+config\s+(use-context|get-contexts|current-context)(?:\s+[^\s]+)*$"
            },
            "helm": {
                "list": r"^helm\s+list(\s+--all-namespaces|\s+-n\s+\w+)?$",
                "install": r"^helm\s+install\s+\w+\s+\S+(\s+--namespace\s+\w+|\s+--set\s+\S+)*$",
                "uninstall": r"^helm\s+uninstall\s+\w+(\s+--namespace\s+\w+)?$",
                "upgrade": r"^helm\s+upgrade\s+\w+\s+\S+(\s+--namespace\s+\w+|\s+--set\s+\S+)*$"
            }
        }

        # Define pod state specific commands
        self.pod_state_commands = {
            "Pending": [
                "kubectl describe nodes",
                "kubectl get events --field-selector=reason=FailedScheduling",
                "kubectl get pods --field-selector=status.phase=Pending -o wide"
            ],
            "CrashLoopBackOff": [
                "kubectl logs {pod} --previous",
                "kubectl describe pod {pod}",
                "kubectl get pod {pod} -o yaml"
            ],
            "ImagePullBackOff": [
                "kubectl describe pod {pod}",
                "kubectl get secrets",
                "kubectl get events --field-selector=reason=Failed"
            ]
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
            r".*--force.*",
            r".*--grace-period=0.*",
            r".*--now.*",
            r".*--cascade=orphan.*",
            r".*delete.*--all.*",
            r".*delete.*--selector=.*",
            r".*delete.*--field-selector=.*",
            r".*--all-namespaces.*delete.*",
            r".*--dry-run=server.*",
            r".*--server-side.*",
            r".*--force-conflicts.*",
            r".*--validate=false.*"
        ]

    def _analyze_pod_state(self, output: str) -> Dict[str, Any]:
        """Analyze pod state from command output"""
        analysis = {
            "state": None,
            "issues": [],
            "recommendations": []
        }

        # Check for Pending state
        if "Pending" in output:
            analysis["state"] = "Pending"
            if "Insufficient" in output:
                analysis["issues"].append("Insufficient resources")
                analysis["recommendations"].append("Check node capacity and resource requests")
            if "FailedScheduling" in output:
                analysis["issues"].append("Scheduling failure")
                analysis["recommendations"].append("Check node taints and pod tolerations")

        # Check for CrashLoopBackOff
        elif "CrashLoopBackOff" in output:
            analysis["state"] = "CrashLoopBackOff"
            if "Error" in output:
                analysis["issues"].append("Container error")
                analysis["recommendations"].append("Check container logs and configuration")

        # Check for ImagePullBackOff
        elif "ImagePullBackOff" in output:
            analysis["state"] = "ImagePullBackOff"
            if "not found" in output:
                analysis["issues"].append("Image not found")
                analysis["recommendations"].append("Verify image name and registry access")
            if "unauthorized" in output:
                analysis["issues"].append("Registry authentication failed")
                analysis["recommendations"].append("Check image pull secrets")

        return analysis

    async def execute_command(self, command: str) -> Dict[str, Any]:
        """Execute a kubectl command and return the result"""
        try:
            # Validate the command first
            if not await self.validate_command(command):
                return {
                    "success": False,
                    "error": "Invalid command"
                }

            # Check if this is a chained command
            if "&&" in command:
                # Split the command into parts
                command_parts = [part.strip() for part in command.split("&&")]
                combined_output = []
                combined_analysis = {
                    "state": None,
                    "issues": [],
                    "recommendations": [],
                    "partial_success": False
                }
                any_success = False

                # Execute each part separately
                for part in command_parts:
                    logger.info(f"Executing command part: {part}")
                    process = await asyncio.create_subprocess_shell(
                        part,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, stderr = await process.communicate()

                    if process.returncode == 0:
                        output = stdout.decode().strip()
                        if output:  # Only add non-empty output
                            combined_output.append(f"=== Output from: {part} ===\n{output}")
                        any_success = True
                        # Analyze this part's output
                        part_analysis = await self._analyze_pod_state(part, output)
                        # Merge analysis results
                        if part_analysis["state"]:
                            combined_analysis["state"] = part_analysis["state"]
                        combined_analysis["issues"].extend(part_analysis["issues"])
                        combined_analysis["recommendations"].extend(part_analysis["recommendations"])
                        if "details" in part_analysis:
                            combined_analysis["details"] = part_analysis["details"]
                    else:
                        error = stderr.decode().strip()
                        if error:  # Only add non-empty errors
                            combined_output.append(f"=== Error from: {part} ===\n{error}")
                        logger.warning(f"Command part failed: {error}")

                # Combine all outputs
                final_output = "\n\n".join(combined_output)

                # Set partial success flag if some commands succeeded
                if any_success:
                    combined_analysis["partial_success"] = True
                    if not combined_analysis["issues"]:
                        combined_analysis["issues"].append("Some commands succeeded but others failed")
                        combined_analysis["recommendations"].append("Review the output for each command part")

                return {
                    "success": any_success,  # Consider it a success if any part succeeded
                    "raw_output": final_output,
                    "output": final_output,
                    "analysis": combined_analysis
                }

            else:
                # Execute single command
                logger.info(f"Executing command: {command}")
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await process.communicate()

                # Check if the command was successful
                if process.returncode == 0:
                    output = stdout.decode().strip()
                    logger.info(f"Command executed successfully. Output: {output}")

                    # Analyze the output for pod states
                    analysis = await self._analyze_pod_state(output)

                    return {
                        "success": True,
                        "raw_output": output,
                        "output": output,
                        "analysis": analysis
                    }
                else:
                    error = stderr.decode().strip()
                    logger.error(f"Command failed: {error}")
                    return {
                        "success": False,
                        "error": error,
                        "raw_output": error,
                        "analysis": {"state": "Error", "issues": [error], "recommendations": ["Check command syntax and permissions"]}
                    }

        except Exception as e:
            error_msg = f"Error executing command: {str(e)}"
            logger.error(error_msg)
            return {
                "success": False,
                "error": error_msg,
                "raw_output": error_msg,
                "analysis": {"state": "Error", "issues": [str(e)], "recommendations": ["Check system configuration"]}
            }

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
                },
                "pod_states": list(self.pod_state_commands.keys())
            }
        except Exception as e:
            logger.error(f"Error getting Kubernetes service info: {str(e)}")
            raise

    async def handle_command(self, command: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """Handle a command by executing it directly"""
        try:
            result = await self.execute_command(command)
            if not result["success"]:
                raise ValueError(f"Command failed: {result['error']}")

            return {
                "status": "success",
                "output": result["output"],
                "command": command,
                "analysis": result.get("analysis")
            }

        except Exception as e:
            logger.error(f"Error handling command {command}: {str(e)}")
            raise

    async def validate_command(self, command: str) -> bool:
        """Validate if a command can be handled by this service"""
        try:
            # Skip validation in permissive mode
            if self.security_mode == SecurityMode.PERMISSIVE:
                return True

            # Check forbidden patterns first
            for pattern in self.forbidden_patterns:
                if re.match(pattern, command, re.IGNORECASE):
                    logger.warning(f"Command matches forbidden pattern: {pattern}")
                    return False

            # Check allowed commands
            for tool, commands in self.allowed_commands.items():
                for cmd, pattern in commands.items():
                    if re.match(pattern, command, re.IGNORECASE):
                        return True

            logger.warning(f"Command not in allowed patterns: {command}")
            return False

        except Exception as e:
            logger.error(f"Error validating command: {str(e)}")
            return False

    async def _analyze_pod_state(self, command: str, output: str = "") -> Dict[str, Any]:
        """Analyze pod state from command output"""
        try:
            analysis = {
                "state": None,
                "issues": [],
                "recommendations": []
            }

            # If no output provided, return empty analysis
            if not output:
                return analysis

            # Check for ContainerCreating state
            if "ContainerCreating" in output:
                analysis["state"] = "ContainerCreating"
                pod_name = None
                namespace = None

                # Extract pod name and namespace from the command
                if "describe pods" in command:
                    # Extract namespace from -n flag
                    if "-n" in command:
                        namespace = command.split("-n")[1].split()[0].strip()
                    # Extract pod name from the command
                    if "-l" in command:
                        # If using label selector, we need to get pod names
                        pod_list_cmd = f"kubectl get pods -n {namespace} -l app=nr-ebpf-agent -o jsonpath='{{.items[*].metadata.name}}'"
                        pod_names = await self.execute_command(pod_list_cmd)
                        if pod_names.success:
                            pod_name = pod_names.output.split()[0]  # Get first pod
                    else:
                        # Extract pod name directly
                        pod_name = command.split("describe pods")[1].strip().split()[0]

                if pod_name and namespace:
                    # Get detailed pod information
                    describe_cmd = f"kubectl describe pod {pod_name} -n {namespace}"
                    describe_output = await self.execute_command(describe_cmd)

                    # Get events for the namespace
                    events_cmd = f"kubectl get events -n {namespace} --sort-by='.lastTimestamp'"
                    events_output = await self.execute_command(events_cmd)

                    # Check for common ContainerCreating issues
                    if "ImagePullBackOff" in describe_output.output:
                        analysis["issues"].append("Container image pull failed")
                        analysis["recommendations"].extend([
                            "Check if the image exists in the registry",
                            "Verify image pull secrets are configured correctly",
                            "Check network connectivity to the container registry"
                        ])

                    if "FailedScheduling" in events_output.output:
                        analysis["issues"].append("Pod scheduling failed")
                        analysis["recommendations"].extend([
                            "Check node resource availability",
                            "Verify node selectors and affinity rules",
                            "Check for taints and tolerations"
                        ])

                    if "FailedMount" in describe_output.output:
                        analysis["issues"].append("Volume mount failed")
                        analysis["recommendations"].extend([
                            "Check if the volume exists",
                            "Verify volume mount permissions",
                            "Check for storage class issues"
                        ])

                    if "CrashLoopBackOff" in describe_output.output:
                        analysis["issues"].append("Container is crashing")
                        analysis["recommendations"].extend([
                            "Check container logs for errors",
                            "Verify container configuration",
                            "Check resource limits and requests"
                        ])

                    if not analysis["issues"]:
                        analysis["issues"].append("Container is still being created")
                        analysis["recommendations"].extend([
                            "Check pod events for more details",
                            "Verify container image and registry access",
                            "Check for resource constraints"
                        ])

                    analysis["details"] = {
                        "pod_name": pod_name,
                        "namespace": namespace,
                        "events": events_output.output if events_output.success else "Could not fetch events",
                        "pod_details": describe_output.output if describe_output.success else "Could not fetch pod details"
                    }

            # Check for Pending state
            elif "Pending" in output:
                analysis["state"] = "Pending"
                if "Insufficient" in output:
                    analysis["issues"].append("Insufficient resources")
                    analysis["recommendations"].append("Check node capacity and resource requests")
                if "FailedScheduling" in output:
                    analysis["issues"].append("Scheduling failure")
                    analysis["recommendations"].append("Check node taints and pod tolerations")

            # Check for CrashLoopBackOff
            elif "CrashLoopBackOff" in output:
                analysis["state"] = "CrashLoopBackOff"
                if "Error" in output:
                    analysis["issues"].append("Container error")
                    analysis["recommendations"].append("Check container logs and configuration")

            # Check for ImagePullBackOff
            elif "ImagePullBackOff" in output:
                analysis["state"] = "ImagePullBackOff"
                if "not found" in output:
                    analysis["issues"].append("Image not found")
                    analysis["recommendations"].append("Verify image name and registry access")
                if "unauthorized" in output:
                    analysis["issues"].append("Registry authentication failed")
                    analysis["recommendations"].append("Check image pull secrets")

            return analysis

        except Exception as e:
            logger.error(f"Error analyzing pod state: {str(e)}")
            return {
                "state": "Error",
                "issues": [f"Error analyzing pod state: {str(e)}"],
                "recommendations": ["Check command syntax and permissions"]
            }
