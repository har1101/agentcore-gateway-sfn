"""Probe the connector's guardrails and error behaviour. Appends to results/errors.jsonl."""
import json
import time

import httpx

from common import ROOT, call_tool, load_config, session, tool_text

cfg = load_config()
sfn = session.client("stepfunctions")
sqs = session.client("sqs")
state = json.loads((ROOT / "results" / "exercise_state.json").read_text())
LOG = ROOT / "results" / "errors.jsonl"
LOG.write_text("")
sm = cfg["stateMachines"]


def tool(step: str, name: str, args: dict):
    resp = call_tool(name, args)
    with LOG.open("a") as f:
        f.write(json.dumps({"step": step, "tool": name, "arguments": args, "response": resp}, ensure_ascii=False) + "\n")
    is_err = resp.get("result", {}).get("isError") or "error" in resp
    print(f"\n### [{step}] {name} -> {'ERROR' if is_err else 'ok'}")
    print(json.dumps(tool_text(resp), ensure_ascii=False, default=str)[:700])
    return resp


def next_task(execution_arn: str) -> str:
    for _ in range(20):
        for m in sqs.receive_message(QueueUrl=cfg["queueUrl"], WaitTimeSeconds=5, MaxNumberOfMessages=10).get("Messages", []):
            sqs.delete_message(QueueUrl=cfg["queueUrl"], ReceiptHandle=m["ReceiptHandle"])
            body = json.loads(m["Body"])
            if body["executionArn"] == execution_arn:
                return body["taskToken"]
    raise RuntimeError("no task")


# --- Pinned / hidden parameters -------------------------------------------------------------------
tool("E1 override pinned stateMachineArn", "orders___StartExecution",
     {"input": "{}", "stateMachineArn": sm["OutOfScope"]})
tool("E2 set hidden includedData", "refund___DescribeStateMachine", {"includedData": "METADATA_ONLY"})
tool("E3 set non-exposed field (StopExecution.error)", "orders___StopExecution",
     {"executionArn": state["standardExecutionArn"], "error": "x"})
tool("E4 non-exposed field on StartExecution (name)", "orders___StartExecution", {"input": "{}", "name": "x"})
tool("E5 invalid enum (statusFilter)", "orders___ListExecutions", {"statusFilter": "DONE"})
tool("E6 missing required (executionArn)", "orders___DescribeExecution", {})
tool("E7 input is not JSON", "orders___StartExecution", {"input": "not json"})
tool("E8 unknown tool", "orders___DeleteStateMachine", {})

# --- Standard / Express constraints --------------------------------------------------------------
tool("S1 StartSyncExecution pinned to Standard", "misconfig___StartSyncExecution", {"input": "{}"})
tool("S2 ListExecutions pinned to Express", "misconfig___ListExecutions", {})
tool("S3 DescribeExecution on Express execution ARN", "orders___DescribeExecution",
     {"executionArn": state["expressExecutionArn"]})
tool("S4 GetExecutionHistory on Express execution ARN", "orders___GetExecutionHistory",
     {"executionArn": state["expressExecutionArn"]})
tool("S5 DescribeStateMachine without pin", "misconfig___DescribeStateMachine", {})

# --- IAM boundary (service role does not allow OutOfScope) ----------------------------------------
tool("I1 StartExecution pinned to a state machine outside the role policy", "misconfig___StartExecution", {"input": "{}"})
oos = sfn.start_execution(stateMachineArn=sm["OutOfScope"])["executionArn"]
tool("I2 DescribeExecution on out-of-scope execution", "orders___DescribeExecution", {"executionArn": oos})
tool("I3 StopExecution on out-of-scope execution", "orders___StopExecution", {"executionArn": oos})
sfn.stop_execution(executionArn=oos)

# --- Task tokens -----------------------------------------------------------------------------------
tool("T1 SendTaskSuccess with bogus token", "tasks___SendTaskSuccess", {"taskToken": "AAAA" * 20, "output": "{}"})
tool("T2 SendTaskSuccess output not JSON", "tasks___SendTaskSuccess",
     {"taskToken": state["usedTaskToken"], "output": "approved"})

# Non-ASCII error/cause: gateway vs direct API
ex1 = sfn.start_execution(stateMachineArn=sm["RefundApproval"], input=json.dumps({"orderId": "U1", "amount": 1}))["executionArn"]
tool("T3 SendTaskFailure with Japanese cause via gateway", "tasks___SendTaskFailure",
     {"taskToken": next_task(ex1), "error": "日本語エラー", "cause": "自己都合の返金は対象外"})
ex2 = sfn.start_execution(stateMachineArn=sm["RefundApproval"], input=json.dumps({"orderId": "U2", "amount": 1}))["executionArn"]
sfn.send_task_failure(taskToken=next_task(ex2), error="日本語エラー", cause="自己都合の返金は対象外")
time.sleep(2)
for label, arn in (("gateway", ex1), ("direct boto3", ex2)):
    d = sfn.describe_execution(executionArn=arn)
    print(f"\n[T3 compare] {label}: error={d.get('error')!r} cause={d.get('cause')!r}")
    with LOG.open("a") as f:
        f.write(json.dumps({"step": f"T3 compare {label}", "error": d.get("error"), "cause": d.get("cause")}, ensure_ascii=False) + "\n")

# --- Unsigned request -----------------------------------------------------------------------------
r = httpx.post(cfg["gatewayUrl"], json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
print(f"\n### [A1 unsigned request] HTTP {r.status_code} {r.text[:300]}")
with LOG.open("a") as f:
    f.write(json.dumps({"step": "A1 unsigned request", "status": r.status_code, "body": r.text[:500]}) + "\n")
