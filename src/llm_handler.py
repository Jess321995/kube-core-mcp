from typing import Dict, Any, List, Optional
from loguru import logger
import boto3
import json
import os
from botocore.exceptions import ClientError
from service_handler import ServiceHandler

class LLMHandler(ServiceHandler):
    """Handler for LLM-based command understanding and processing using AWS Bedrock"""

    def __init__(self):
        """Initialize the LLM handler with AWS Bedrock"""
        try:
            # Initialize AWS Bedrock client
            self.bedrock = boto3.client(
                service_name='bedrock-runtime',
                region_name=os.getenv('AWS_REGION', 'us-west-2')
            )

            # Select model based on provider
            self.model_id = os.getenv('LLM_MODEL_ID', 'anthropic.claude-3-sonnet-20240229-v1:0')
            self.provider = self.model_id.split('.')[0]

            # Load the prompt template
            self.prompt_template = self._load_prompt_template()

            # Verify model access
            self._verify_model_access()

            logger.info(f"Initialized LLM handler with model {self.model_id}")

        except Exception as e:
            logger.error(f"Error initializing LLM handler: {str(e)}")
            raise

    def _load_prompt_template(self) -> str:
        """Load the prompt template from PROMPT.md"""
        try:
            prompt_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'PROMPT.md')
            with open(prompt_path, 'r') as f:
                content = f.read()
                # Extract the prompt from the markdown file
                # The prompt is between the first and second ``` markers
                prompt = content.split('```')[1].strip()
                return prompt
        except Exception as e:
            logger.error(f"Error loading prompt template: {str(e)}")
            # Fallback to hardcoded prompt if file reading fails
            return """You are a Kubernetes expert. Convert the following request into a kubectl or helm command.
            Rules:
            1. Only output the command, no explanations
            2. Use standard kubectl/helm syntax
            3. Include all necessary flags and parameters
            4. For create commands, use appropriate defaults if not specified
            5. For deployments, always include --image flag
            6. For services, always include --port flag
            7. ALWAYS include namespace:
               - Use -n <namespace> if a specific namespace is mentioned
               - Use --all-namespaces if "all namespaces" is mentioned
               - Use -n default if no namespace is specified
            8. For security, never use --privileged or other dangerous flags
            9. For get commands, follow these patterns exactly:
               - kubectl get pods -n <namespace>
               - kubectl get pods --all-namespaces
               - kubectl get pods <pod-name> -n <namespace>
            10. Never omit the namespace flag

            Request: {message}

            Command:"""

    def _verify_model_access(self):
        """Verify that we can access the selected model"""
        try:
            # Try a simple prompt to verify access
            test_prompt = "Hello, are you working?"
            self._call_llm(test_prompt)
            logger.info("Successfully verified LLM access")
        except Exception as e:
            logger.error(f"Failed to verify LLM access: {str(e)}")
            raise

    def _call_llm(self, prompt: str) -> str:
        """Call the LLM with a prompt and return the response"""
        try:
            if self.provider == 'anthropic':
                response = self.bedrock.invoke_model(
                    modelId=self.model_id,
                    body=json.dumps({
                        "anthropic_version": "bedrock-2023-05-31",
                        "max_tokens": 1024,
                        "messages": [{
                            "role": "user",
                            "content": prompt
                        }]
                    })
                )
                response_body = json.loads(response['body'].read())
                return response_body['content'][0]['text']
            else:
                raise ValueError(f"Unsupported LLM provider: {self.provider}")

        except Exception as e:
            logger.error(f"Error calling LLM: {str(e)}")
            raise

    async def understand_command(self, message: str) -> Dict[str, Any]:
        """Convert natural language to a kubectl command"""
        try:
            # Use the prompt template with the message
            prompt = self.prompt_template.format(message=message)

            # Get the command from the LLM
            command = self._call_llm(prompt).strip()

            # Basic validation of the command
            if not command.startswith(('kubectl ', 'helm ')):
                return {
                    "success": False,
                    "error": "Generated command is not a valid kubectl or helm command"
                }

            # Ensure get commands include namespace
            if command.startswith('kubectl get '):
                if '-n ' not in command and '--all-namespaces' not in command:
                    # Add default namespace if missing
                    command = f"{command} -n default"

            return {
                "success": True,
                "command": command
            }

        except Exception as e:
            logger.error(f"Error understanding command: {str(e)}")
            return {
                "success": False,
                "error": str(e)
            }

    async def get_service_info(self) -> Dict[str, Any]:
        """Get information about the LLM service"""
        return {
            "name": "llm",
            "provider": self.provider,
            "model": self.model_id,
            "capabilities": ["command_generation"]
        }

    async def handle_command(self, command: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """Handle LLM commands"""
        if command == "understand":
            return await self.understand_command(parameters.get("text", ""))
        else:
            raise ValueError(f"Unknown LLM command: {command}")

    async def validate_command(self, command: str, parameters: Dict[str, Any]) -> bool:
        """Validate if a command can be handled by the LLM service"""
        try:
            # For LLM handler, we only support the 'understand' command
            valid_commands = ["understand"]
            if command not in valid_commands:
                logger.warning(f"Invalid LLM command: {command}")
                return False

            # For 'understand' command, we need a 'text' parameter
            if command == "understand" and "text" not in parameters:
                logger.warning("Missing required 'text' parameter for understand command")
                return False

            return True
        except Exception as e:
            logger.error(f"Error validating LLM command: {str(e)}")
            return False
