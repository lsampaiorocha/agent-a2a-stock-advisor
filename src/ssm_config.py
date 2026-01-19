import os
import boto3
from functools import lru_cache

def _bootstrap_region() -> str:
    # Need a region to call SSM; AgentCore typically sets this automatically.
    return os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-2"

@lru_cache(maxsize=1)
def _ssm():
    return boto3.client("ssm", region_name=_bootstrap_region())

@lru_cache(maxsize=128)
def get_ssm(name: str, default: str = "", decrypt: bool = True) -> str:
    try:
        resp = _ssm().get_parameter(Name=name, WithDecryption=decrypt)
        return resp["Parameter"]["Value"]
    except Exception:
        return default
