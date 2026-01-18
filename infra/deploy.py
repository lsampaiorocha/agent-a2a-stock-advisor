
import os
import sys
import boto3
from boto3.session import Session
from bedrock_agentcore_starter_toolkit import Runtime
import logging

# Add root infra to path to allow importing shared utils
# This assumes the script is run from the root of the repo or we adjust path relative to this file
# Repo structure:
# repo/
#   infra/utils.py
#   agents/aws_doc_expert/infra/deploy.py

# Path to infra folder from here: ../../../infra
current_dir = os.path.dirname(os.path.abspath(__file__))
infra_dir = os.path.abspath(os.path.join(current_dir, "../../../infra"))
sys.path.append(infra_dir)

from utils import (
    create_agentcore_runtime_execution_role,
    put_ssm_parameter,
    AWS_DOCS_ROLE_NAME,
    SSM_DOCS_AGENT_ARN,
    setup_cognito_user_pool
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def deploy(client_id=None, discovery_url=None):
    print("📚 Deploying AWS Docs Expert Agent...")
    session = Session()
    region = 'us-east-2'
    
    # Ensure dependencies are available (Cognito)
    # If not passed, we try to setup/get them
    if not client_id or not discovery_url:
        cognito_config = setup_cognito_user_pool()
        client_id = cognito_config.get("client_id")
        discovery_url = cognito_config.get("discovery_url")

    # Create Role
    role_arn = create_agentcore_runtime_execution_role(AWS_DOCS_ROLE_NAME)
    
    # Agent Source Path
    # agents/aws_doc_expert/src/agent.py
    # This script is in agents/aws_doc_expert/infra/deploy.py
    # So relative path to agent.py from CWD (root) must be set correctly by the caller 
    # OR we use absolute paths.
    # The Runtime starter toolkit expects paths relative to CWD usually.
    # We will assume this is run from ROOT repo `python agents/aws_doc_expert/infra/deploy.py`
    
    entrypoint = "agents/aws_doc_expert/src/agent.py"
    req_file = "requirements.txt"

    runtime = Runtime()
    runtime.configure(
        entrypoint=entrypoint,
        execution_role=role_arn,
        auto_create_ecr=True,
        requirements_file=req_file,
        region=region,
        agent_name="aws_docs_assistant",
        authorizer_configuration={
            "customJWTAuthorizer": {
                "allowedClients": [client_id],
                "discoveryUrl": discovery_url,
            }
        },
        protocol="HTTP",
    )
    launch = runtime.launch()
    print(f"✅ AWS Docs Agent Deployed: {launch.agent_arn}")
    put_ssm_parameter(SSM_DOCS_AGENT_ARN, launch.agent_arn)
    return launch.agent_arn

if __name__ == "__main__":
    deploy()
