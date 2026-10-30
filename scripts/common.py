"""Shared helpers: config file and a SigV4-signed MCP (JSON-RPC over HTTP) client for the gateway."""
import itertools
import json
import os
from pathlib import Path

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

REGION = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or "ap-northeast-1"
PREFIX = "AgcDemo"
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"

session = boto3.Session(region_name=REGION)


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")


_ids = itertools.count(1)


def mcp(method: str, params: dict | None = None, url: str | None = None) -> dict:
    """Send one JSON-RPC request to the gateway, signed with the caller's AWS credentials."""
    url = url or load_config()["gatewayUrl"]
    body = json.dumps({"jsonrpc": "2.0", "id": next(_ids), "method": method, "params": params or {}})
    req = AWSRequest(method="POST", url=url, data=body, headers={"Content-Type": "application/json"})
    SigV4Auth(session.get_credentials().get_frozen_credentials(), "bedrock-agentcore", REGION).add_auth(req)
    resp = httpx.post(url, content=body, headers=dict(req.headers), timeout=120)
    return resp.json()


def call_tool(name: str, arguments: dict) -> dict:
    return mcp("tools/call", {"name": name, "arguments": arguments})


def tool_text(resp: dict):
    """Return the parsed JSON text payload of a tools/call result (or the raw text / error object)."""
    if "error" in resp:
        return resp["error"]
    text = resp["result"]["content"][0]["text"]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text
