
import os
import sys
import boto3
import logging

# Add root infra to path
current_dir = os.path.dirname(os.path.abspath(__file__))
infra_dir = os.path.abspath(os.path.join(current_dir, "../../../infra"))
sys.path.append(infra_dir)

from utils import (
    delete_agentcore_runtime_execution_role,
    delete_ssm_parameter,
    runtime_resource_cleanup,
    short_memory_cleanup,
    delete_observability_resources,
    AWS_DOCS_ROLE_NAME,
    SSM_DOCS_AGENT_ARN
)

logging.basicConfig(level=logging.INFO)

def destroy():
    print("🔥 Destroying AWS Docs Expert Agent...")
    
    # 1. Delete Runtime
    client = boto3.client("bedrock-agentcore-control")
    try:
        runtimes = client.list_agent_runtimes()
        for rt in runtimes["agentRuntimes"]:
            if rt["agentRuntimeName"] == "aws_docs_assistant":
                print(f"🗑️ Deleting Runtime: {rt['agentRuntimeName']}")
                runtime_resource_cleanup(rt['agentRuntimeId'])
    except Exception as e:
        print(f"⚠️ Error listing/deleting runtimes: {e}")

    # 2. Delete Role
    delete_agentcore_runtime_execution_role(AWS_DOCS_ROLE_NAME)
    
    # 3. Delete SSM
    delete_ssm_parameter(SSM_DOCS_AGENT_ARN)
    
    # 4. Cleanup Logs/Memory
    short_memory_cleanup("aws_docs_assistant")
    delete_observability_resources("aws_docs_assistant")

    print("✅ AWS Docs Expert Agent Destroyed")

if __name__ == "__main__":
    destroy()
