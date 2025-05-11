from typing import Dict, Any, List, Optional
from loguru import logger
import boto3
import json
import os
from botocore.exceptions import ClientError
from service_handler import ServiceHandler
from dataclasses import dataclass
from datetime import datetime

@dataclass
class ConversationContext:
    """Maintains conversation context for better command generation"""
    messages: List[Dict[str, str]]
    last_command: Optional[str]
    last_output: Optional[str]
    timestamp: datetime

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

            # Initialize conversation context
            self.conversation_context = ConversationContext(
                messages=[],
                last_command=None,
                last_output=None,
                timestamp=datetime.now()
            )

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
            return """You are a Kubernetes expert. Your role is to help users understand and troubleshoot their Kubernetes clusters by converting their questions into appropriate kubectl commands.

            IMPORTANT: You must output ONLY a single kubectl command or a chain of kubectl commands joined by &&. Do not include explanations, steps, or markdown formatting.

            Core Principles:
            1. Security First
               - Never use dangerous flags like --privileged
               - Avoid commands that could expose sensitive data
               - Use appropriate RBAC permissions

            2. Context Awareness
               - Always consider namespace context
               - Use --all-namespaces when appropriate
               - Include relevant labels and selectors

            3. Troubleshooting Approach
               - Start with basic resource inspection
               - Progress to detailed diagnostics when needed
               - Use appropriate output formats (-o wide, -o yaml, etc.)
               - Chain commands with && when multiple commands are needed

            4. Common Patterns
               - For resource discovery: get -> describe -> logs
               - For debugging: events -> logs -> describe
               - For status checks: get with appropriate selectors
               - For configuration: get with -o yaml

            5. Error Investigation
               - Check pod status and conditions
               - Review container logs
               - Examine events
               - Verify resource availability
               - Check configuration issues

            6. Best Practices
               - Use field selectors for efficient filtering
               - Include relevant labels for better identification
               - Use appropriate output formats for readability
               - Chain commands with && when needed for complete information

            When handling requests:
            1. Understand the user's intent
            2. Choose appropriate commands based on context
            3. Include necessary flags and parameters
            4. Ensure commands follow security best practices
            5. Provide complete information for troubleshooting

            For "why" questions:
            1. Gather diagnostic information
            2. Check relevant logs and events
            3. Examine resource configuration
            4. Look for common failure patterns
            5. Consider cluster-wide issues

            For troubleshooting:
            1. Identify the affected resources
            2. Check resource status and conditions
            3. Review relevant logs and events
            4. Examine configuration and dependencies
            5. Consider cluster-wide factors

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

    async def _call_llm(self, prompt: str) -> str:
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
                        }],
                        "temperature": 0.1  # Lower temperature for more consistent output
                    })
                )
                response_body = json.loads(response['body'].read())
                logger.debug(f"Raw LLM response: {json.dumps(response_body, indent=2)}")

                # Check if we have the expected response structure
                if 'content' not in response_body or not response_body['content']:
                    raise ValueError(f"Unexpected response structure: {response_body}")

                # Return the full response text
                response_text = response_body['content'][0]['text'].strip()
                logger.debug(f"Full LLM response: {response_text}")
                return response_text

            else:
                raise ValueError(f"Unsupported LLM provider: {self.provider}")

        except Exception as e:
            logger.error(f"Error calling LLM: {str(e)}")
            raise

    def _build_context(self, message: str) -> str:
        """Build context from conversation history"""
        context = []

        # Add last command and output if available
        if self.conversation_context.last_command:
            context.append(f"Last command executed: {self.conversation_context.last_command}")
        if self.conversation_context.last_output:
            context.append(f"Last command output: {self.conversation_context.last_output}")

        # Add relevant conversation history
        for msg in self.conversation_context.messages[-3:]:  # Keep last 3 messages
            context.append(f"{msg['role']}: {msg['content']}")

        return "\n".join(context)

    async def understand_command(self, message: str) -> Dict[str, Any]:
        """Convert natural language to kubectl command"""
        try:
            # Update conversation context
            self.conversation_context.messages.append({
                "role": "user",
                "content": message
            })

            # Build context-aware prompt
            context = self._build_context(message)
            prompt = f"{self.prompt_template}\n\nContext:\n{context}\n\nUser: {message}\nCommand:"

            # Call the LLM
            response = await self._call_llm(prompt)

            # Extract the command from the response
            command = response.strip()

            # Update conversation context with the command
            self.conversation_context.last_command = command

            return {
                "success": True,
                "command": command,
                "response": response
            }

        except Exception as e:
            logger.error(f"Error understanding command: {str(e)}")
            return {
                "success": False,
                "error": str(e)
            }

    async def summarize_output(self, output: str) -> str:
        """Summarize the command output using the LLM"""
        try:
            # Update conversation context with the output
            self.conversation_context.last_output = output

            # Check for ContainerCreating state
            if "ContainerCreating" in output:
                # Build a more detailed prompt for ContainerCreating analysis
                prompt = f"""You are a Kubernetes expert. Analyze the following output and provide a detailed analysis focusing on:
                1. The specific reason why pods are stuck in ContainerCreating state
                2. Common causes for ContainerCreating issues:
                   - Image pull issues (missing image, registry access)
                   - Volume mount problems
                   - Resource constraints
                   - Network configuration issues
                   - Security context issues
                3. Specific steps to troubleshoot and resolve the issue
                4. Commands that would help diagnose the root cause

                Output to analyze:
                {output}

                Provide a structured response with:
                1. Problem Analysis
                2. Likely Causes
                3. Troubleshooting Steps
                4. Diagnostic Commands
                """

                summary = await self._call_llm(prompt)
                return summary.strip()

            # For other types of output, use the existing logic
            if not any(keyword in output.lower() for keyword in ['error', 'warning', 'failed', 'not found', 'crash', 'exception', 'pending']):
                return "No issues found in the output."

            # Build context-aware prompt for summarization
            context = self._build_context("Summarize the following output:")
            prompt = f"""You are a Kubernetes expert. Analyze the following kubectl command output and provide a concise summary focusing ONLY on:
            - Error messages and their root causes
            - Warning signs
            - Failed operations
            - Resource not found issues
            - Pod state issues (especially Pending)
            - Any other problems that need attention

            Context:
            {context}

            Command output:
            {output}

            Summary:"""

            summary = await self._call_llm(prompt)
            return summary.strip()

        except Exception as e:
            logger.error(f"Error summarizing output: {str(e)}")
            return f"Error summarizing output: {str(e)}"

    async def get_service_info(self) -> Dict[str, Any]:
        """Get information about the LLM service"""
        return {
            "name": "llm",
            "model": self.model_id,
            "capabilities": [
                "natural_language_understanding",
                "command_generation",
                "output_summarization",
                "context_aware_responses"
            ]
        }

    async def handle_command(self, command: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """Handle a command by understanding it first"""
        try:
            result = await self.understand_command(command)
            if not result["success"]:
                raise ValueError(f"Failed to understand command: {result.get('error')}")
            return result
        except Exception as e:
            logger.error(f"Error handling command: {str(e)}")
            raise

    async def validate_command(self, command: str, parameters: Dict[str, Any]) -> bool:
        """Validate if a command can be handled by this service"""
        return True  # LLM can handle any natural language input
