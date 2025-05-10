# Kube Core MCP

A Kubernetes command processing service that converts natural language requests into valid kubectl commands.

## Features

- Natural language to kubectl command conversion
- Command validation and security checks
- Support for common kubectl operations
- AWS Bedrock integration for LLM processing

## Prerequisites

- Python 3.8+
- AWS credentials configured
- kubectl installed and configured
- Node.js and npm (for frontend)

## Setup

1. Clone the repository:
```bash
git clone <repository-url>
cd kube-core-mcp
```

2. Create and activate a virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: .\venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Configure AWS credentials:
```bash
export AWS_ACCESS_KEY_ID=your_access_key
export AWS_SECRET_ACCESS_KEY=your_secret_key
export AWS_REGION=your_region
```

5. Start the FastAPI server:
```bash
python src/server.py
```

## API Documentation

### Health Check
```bash
curl http://localhost:3000/health
```

### Services
```bash
curl http://localhost:3000/api/services
```

### Natural Language Commands
```bash
curl -X POST http://localhost:3000/api/nl \
  -H "Content-Type: application/json" \
  -d '{"message": "show me the pods in default namespace"}'
```

### Direct Commands
```bash
curl -X POST http://localhost:3000/api/command \
  -H "Content-Type: application/json" \
  -d '{"command": "kubectl get pods -n default"}'
```

## Security

The service operates in two security modes:

1. STRICT (default):
   - Only allows predefined command patterns
   - Validates all commands against allowed patterns
   - Prevents dangerous operations

2. PERMISSIVE:
   - Allows more flexible command patterns
   - Still maintains basic security checks
   - Useful for development and testing

## Development

### Running Tests
```bash
pytest tests/
```

### Code Style
```bash
black src/ tests/
flake8 src/ tests/
```

### Contributing
1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run tests
5. Submit a pull request

## License

[Add License Information]
