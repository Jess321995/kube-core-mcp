from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from loguru import logger
import uvicorn
import yaml
from typing import Dict, Any, Optional, List, Union
import os
import sys
from pathlib import Path
from kubernetes import client, config
from autogen_core import ComponentModel
from autogen_core.models import SystemMessage, UserMessage
from autogen_core.model_context import ChatCompletionContext
import io
from datetime import datetime

# Add the src directory to Python path
src_path = str(Path(__file__).parent)
if src_path not in sys.path:
    sys.path.append(src_path)

from dotenv import load_dotenv
from kubernetes_handler import KubernetesHandler, SecurityMode
from llm_handler import LLMHandler

# Load environment variables
load_dotenv()

app = FastAPI(title="Kubernetes MCP Server")

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

# Initialize Kubernetes client
try:
    config.load_incluster_config()  # Try loading in-cluster config first
except config.ConfigException:
    try:
        config.load_kube_config()  # Fall back to local kubeconfig
    except config.ConfigException:
        logger.warning("Could not load Kubernetes config. Commands will be simulated.")

k8s_api = client.CoreV1Api()
k8s_apps_api = client.AppsV1Api()

# Initialize service handlers
service_handlers = {
    "kubernetes": KubernetesHandler(security_mode=SecurityMode.STRICT),
    "llm": LLMHandler()
}

# Add a memory handler to capture logs
log_capture = io.StringIO()
logger.add(log_capture, format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}")

# Simplified request models
class Message(BaseModel):
    """Message model for MCP protocol"""
    type: str
    payload: Dict[str, Any]
    metadata: Optional[Dict[str, Any]] = None

class NaturalLanguageRequest(BaseModel):
    message: str
    context: Optional[Dict[str, Any]] = None

class CommandRequest(BaseModel):
    command: str
    parameters: Optional[Dict[str, Any]] = None

class MessageRequest(BaseModel):
    message: str
    namespace: Optional[str] = "default"
    model: Optional[str] = "gpt-4"

# Simplified response models
class ServiceInfo(BaseModel):
    name: str
    capabilities: List[str]
    model: Optional[str] = None

class CommandResult(BaseModel):
    success: bool
    command: str
    output: str
    error: Optional[str] = None

class LogEntry(BaseModel):
    timestamp: str
    level: str
    message: str

class LogResponse(BaseModel):
    logs: List[LogEntry]

@app.get("/")
async def root() -> Dict[str, str]:
    """Root endpoint"""
    return {"status": "running", "service": "core-mcp-server"}

@app.get("/services")
async def list_services() -> Dict[str, Dict[str, Any]]:
    """List available services and their capabilities"""
    services_info = {}
    for service_name, handler in service_handlers.items():
        try:
            info = await handler.get_service_info()
            services_info[service_name] = {
                "name": service_name,
                "capabilities": info.get("capabilities", []),
                "model": info.get("model")
            }
        except Exception as e:
            logger.error(f"Error getting info for service {service_name}: {str(e)}")
            services_info[service_name] = {"error": str(e)}
    return services_info

@app.post("/command")
async def handle_command(request: CommandRequest) -> CommandResult:
    """Handle direct command execution"""
    try:
        # Execute the command directly
        result = await service_handlers["kubernetes"].execute_command(request.command)

        return CommandResult(
            success=result.success,
            command=request.command,
            output=result.output,
            error=result.error
        )

    except Exception as e:
        logger.error(f"Error executing command: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/nl")
async def handle_natural_language(request: NaturalLanguageRequest) -> Dict[str, Any]:
    """Handle natural language commands"""
    try:
        # First, let the LLM understand the command and generate a kubectl command
        understanding = await service_handlers["llm"].understand_command(request.message)

        if not understanding.get("success", False):
            raise HTTPException(status_code=400, detail=understanding.get("error", "Failed to understand command"))

        # Extract the generated kubectl command from the LLM's response
        command = understanding.get("command", "")
        if not command:
            raise HTTPException(status_code=400, detail="No command generated")

        # Execute the command using the Kubernetes handler
        result = await service_handlers["kubernetes"].execute_command(command)

        return {
            "status": "success",
            "message": "Command executed successfully",
            "command": command,
            "output": result.output,
            "error": result.error if not result.success else None
        }

    except Exception as e:
        logger.error(f"Error handling natural language request: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/message")
async def handle_message(message: Message) -> Dict[str, Any]:
    """Handle incoming MCP messages"""
    try:
        logger.info(f"Received message of type: {message.type}")
        # Process message based on type
        if message.type == "ping":
            return {"status": "pong"}
        elif message.type == "echo":
            return {"status": "success", "data": message.payload}
        else:
            raise HTTPException(status_code=400, detail=f"Unknown message type: {message.type}")
    except Exception as e:
        logger.error(f"Error processing message: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/test-llm")
async def test_llm() -> Dict[str, Any]:
    """Test endpoint to verify LLM functionality"""
    try:
        logger.info("Testing LLM functionality...")
        llm_handler = service_handlers["llm"]

        # Test with a simple prompt
        test_prompt = "What is 2+2? Please respond with just the number."

        # Get service info first
        service_info = await llm_handler.get_service_info()
        logger.info(f"LLM Service Info: {service_info}")

        # Test the model
        result = await llm_handler.handle_command(
            "understand",
            {"text": test_prompt}
        )

        return {
            "status": "success",
            "service_info": {
                "name": "llm",
                "capabilities": service_info.get("capabilities", []),
                "model": service_info.get("model")
            },
            "test_prompt": test_prompt,
            "result": result
        }

    except Exception as e:
        logger.error(f"Error testing LLM: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Error testing LLM: {str(e)}"
        )

@app.get("/test-complex")
async def test_complex_command():
    """Test endpoint for complex command understanding"""
    try:
        logger.info("Testing complex command understanding...")

        # Complex test command that involves multiple services
        test_command = """
        Create a new Kubernetes deployment called 'webapp' with 3 replicas,
        using the nginx:latest image, and expose it on port 80.
        Also set up a horizontal pod autoscaler to scale between 2 and 5 replicas
        based on CPU usage of 70%.
        """

        # Get service info
        service_info = await service_handlers['llm'].get_service_info()

        # Process the command
        result = await service_handlers['llm'].handle_command(
            "understand",
            {"text": test_command}
        )

        return {
            "status": "success",
            "service_info": service_info,
            "test_command": test_command,
            "understanding": result
        }
    except Exception as e:
        logger.error(f"Error testing complex command: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/info")
async def get_service_info() -> Dict[str, Any]:
    """Get information about available services"""
    try:
        k8s_info = await service_handlers["kubernetes"].get_service_info()
        return {
            "status": "success",
            "services": {
                "kubernetes": k8s_info
            }
        }
    except Exception as e:
        logger.error(f"Error getting service info: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/convert", response_model=CommandResult)
async def convert_message(request: MessageRequest) -> Dict:
    """
    Convert natural language message to Kubernetes command and execute it
    """
    try:
        # Create system prompt for command generation
        system_prompt = """You are a Kubernetes command generator. Your task is to:
        1. Convert natural language requests into valid kubectl commands
        2. Ensure commands are safe and follow best practices
        3. Only generate commands that can be executed by the user
        4. Return ONLY the command, no explanations

        Example input: "Create a deployment named nginx with 3 replicas"
        Example output: kubectl create deployment nginx --image=nginx --replicas=3
        """

        # Create chat context
        context = ChatCompletionContext()
        await context.add_message(SystemMessage(content=system_prompt))
        await context.add_message(UserMessage(content=request.message, source="user"))

        # Get model response
        model = ComponentModel(name=request.model)
        response = await model.create(messages=await context.get_messages())

        if not isinstance(response.content, str):
            raise ValueError("Model did not return a string command")

        command = response.content.strip()

        # Validate command
        if not command.startswith("kubectl"):
            raise ValueError("Generated command must start with 'kubectl'")

        # Execute command
        try:
            # For now, just return the command without executing
            # TODO: Implement safe command execution
            return CommandResult(
                success=True,
                command=command,
                output="Command generated successfully. Execution not implemented yet.",
            )
        except Exception as e:
            logger.error(f"Error executing command: {str(e)}")
            return CommandResult(
                success=False,
                command=command,
                output="",
                error=f"Error executing command: {str(e)}"
            )

    except Exception as e:
        logger.error(f"Error processing request: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "running", "service": "kube-core-mcp"}

@app.get("/api/logs", response_model=LogResponse)
async def get_logs(limit: int = 100):
    """Get recent logs"""
    try:
        # Get logs from the capture buffer
        log_contents = log_capture.getvalue()
        log_lines = log_contents.strip().split('\n')[-limit:]

        logs = []
        for line in log_lines:
            try:
                # Parse log line: "2024-03-21 10:30:45 | INFO | message"
                timestamp_str, level, message = line.split(' | ', 2)
                logs.append(LogEntry(
                    timestamp=timestamp_str,
                    level=level,
                    message=message
                ))
            except ValueError:
                # Skip malformed log lines
                continue

        return LogResponse(logs=logs)
    except Exception as e:
        logger.error(f"Error retrieving logs: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

def load_config() -> Dict[str, Any]:
    """Load server configuration"""
    config_path = os.getenv("CONFIG_PATH", "config.yaml")
    try:
        with open(config_path, "r") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.warning(f"Config file not found at {config_path}, using defaults")
        return {
            "host": "0.0.0.0",
            "port": 8000,
            "log_level": "INFO"
        }

if __name__ == "__main__":
    config = load_config()
    logger.info(f"Starting server with config: {config}")
    uvicorn.run(
        "server:app",
        host=config["host"],
        port=config["port"],
        log_level=config["log_level"].lower()
    )
