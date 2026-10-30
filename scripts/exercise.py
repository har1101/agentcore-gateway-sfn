"""Exercise all ten Step Functions connector operations through the gateway (MCP tools/call, SigV4).

Every request/response is appended to results/exercise.jsonl for the blog write-up.
"""
import json
import sys
import time

from common import ROOT, call_tool, load_config, session, tool_text

cfg = load_config()
sqs = session.client("sqs")
LOG = ROOT / "results" / "exercise.jsonl"
LOG.parent.mkdir(exist_ok=True)
LOG.write_text("")


def tool(step: str, name: str, args: dict, quiet: bool = False):
    resp = call_tool(name, args)
    result = resp.get("result", {})
    payload = tool_text(resp)
    with LOG.open("a") as f:
        f.write(json.dumps({"step": step, "tool": name, "arguments": args, "response": resp}, ensure_ascii=False) + "\n")
    flag = "ERROR" if result.get("isError") or "error" in resp else "ok"
    print(f"\n### [{step}] {name} {json.dumps(args, ensure_ascii=False)[:200]} -> {flag}")
    if not quiet:
        print(json.dumps(payload, ensure_ascii=False, indent=1, default=str)[:1500])
    return payload


def wait_done(arn: str, label: str) -> dict:
    while True:
        d = tool(label, "orders___DescribeExecution", {"executionArn": arn, "includedData": "ALL_DATA"}, quiet=True)
        if d["status"] != "RUNNING":
            print(json.dumps(d, ensure_ascii=False, indent=1)[:1500])
            return d
        time.sleep(2)


def receive_task(execution_arn: str) -> dict:
    """What the agent's application does: poll SQS for the task token of a given execution."""
    for _ in range(20):
        msgs = sqs.receive_message(QueueUrl=cfg["queueUrl"], WaitTimeSeconds=5, MaxNumberOfMessages=10).get("Messages", [])
        for m in msgs:
            body = json.loads(m["Body"])
            sqs.delete_message(QueueUrl=cfg["queueUrl"], ReceiptHandle=m["ReceiptHandle"])
            if body["executionArn"] == execution_arn:
                print(f"\n(SQS) received task for {execution_arn.split(':')[-1]}: request={body['request']}")
                return body
    raise RuntimeError("task token not received")


ts = time.strftime("%H%M%S")

# --- 1. DescribeStateMachine (pinned) ---------------------------------------------------------------
tool("1 DescribeStateMachine", "orders___DescribeStateMachine", {})
tool("1 DescribeStateMachine ALL_DATA", "orders___DescribeStateMachine", {"includedData": "ALL_DATA"})

# --- 2. StartExecution -> DescribeExecution -> GetExecutionHistory ---------------------------------
ok = tool("2 StartExecution", "orders___StartExecution",
          {"input": json.dumps({"orderId": f"A-{ts}", "amount": 5000, "waitSeconds": 3})})
tool("2 DescribeExecution (METADATA_ONLY)", "orders___DescribeExecution", {"executionArn": ok["executionArn"]})
wait_done(ok["executionArn"], "2 DescribeExecution poll")
hist = tool("2 GetExecutionHistory", "orders___GetExecutionHistory", {"executionArn": ok["executionArn"]}, quiet=True)
print("events:", [e["type"] for e in hist["events"]])

# --- 3. Failing execution (Fail state) ---------------------------------------------------------------
ng = tool("3 StartExecution (amount over limit)", "orders___StartExecution",
          {"input": json.dumps({"orderId": f"B-{ts}", "amount": 200000})})
wait_done(ng["executionArn"], "3 DescribeExecution poll")

# --- 4. StopExecution on a long-running execution ----------------------------------------------------
long = tool("4 StartExecution (waits 300s)", "orders___StartExecution",
            {"input": json.dumps({"orderId": f"C-{ts}", "amount": 1000, "waitSeconds": 300})})
time.sleep(2)
tool("4 StopExecution", "orders___StopExecution", {"executionArn": long["executionArn"]})
wait_done(long["executionArn"], "4 DescribeExecution after stop")
tool("4 StopExecution again (already stopped)", "orders___StopExecution", {"executionArn": long["executionArn"]})

# --- 5. ListExecutions with statusFilter -------------------------------------------------------------
for status in ("SUCCEEDED", "FAILED", "ABORTED"):
    lst = tool(f"5 ListExecutions {status}", "orders___ListExecutions", {"statusFilter": status}, quiet=True)
    print([(e["name"], e["status"]) for e in lst["executions"]][:5])

# --- 6. StartSyncExecution (Express) -----------------------------------------------------------------
sync = tool("6 StartSyncExecution", "lookup___StartSyncExecution", {"input": json.dumps({"orderId": "1001"})})
tool("6 StartSyncExecution (unknown order)", "lookup___StartSyncExecution", {"input": json.dumps({"orderId": "9999"})})
cfg_out = {"expressExecutionArn": sync["executionArn"]}

# --- 7. Callback: StartExecution(name) -> SQS token -> SendTaskHeartbeat -> SendTaskSuccess ---------
name = f"refund-A-{ts}"
refund_input = json.dumps({"orderId": f"A-{ts}", "amount": 5000, "reason": "商品破損"})
r1 = tool("7 StartExecution (refund, named)", "refund___StartExecution", {"input": refund_input, "name": name})
tool("7 StartExecution same name+input (idempotent)", "refund___StartExecution", {"input": refund_input, "name": name})
tool("7 StartExecution same name, different input", "refund___StartExecution",
     {"input": json.dumps({"orderId": "X", "amount": 1}), "name": name})
task = receive_task(r1["executionArn"])
tool("7 SendTaskHeartbeat", "tasks___SendTaskHeartbeat", {"taskToken": task["taskToken"]})
tool("7 SendTaskSuccess", "tasks___SendTaskSuccess",
     {"taskToken": task["taskToken"], "output": json.dumps({"approved": True, "comment": "写真で破損を確認"})})
wait_done(r1["executionArn"], "7 DescribeExecution (refund via orders target)")
tool("7 SendTaskSuccess again (token already used)", "tasks___SendTaskSuccess",
     {"taskToken": task["taskToken"], "output": "{}"})

# --- 8. Callback rejected: SendTaskFailure -----------------------------------------------------------
r2 = tool("8 StartExecution (refund)", "refund___StartExecution",
          {"input": json.dumps({"orderId": f"B-{ts}", "amount": 99999, "reason": "気が変わった"})})
task2 = receive_task(r2["executionArn"])
tool("8 SendTaskFailure", "tasks___SendTaskFailure",
     {"taskToken": task2["taskToken"], "error": "RefundPolicyViolation", "cause": "自己都合の返金は対象外"})
wait_done(r2["executionArn"], "8 DescribeExecution")

# --- 9. Callback approved=false -> Fail state in the workflow ---------------------------------------
r3 = tool("9 StartExecution (refund)", "refund___StartExecution",
          {"input": json.dumps({"orderId": f"C-{ts}", "amount": 3000, "reason": "サイズ違い"})})
task3 = receive_task(r3["executionArn"])
tool("9 SendTaskSuccess approved=false", "tasks___SendTaskSuccess",
     {"taskToken": task3["taskToken"], "output": json.dumps({"approved": False, "comment": "交換で対応"})})
wait_done(r3["executionArn"], "9 DescribeExecution")

# --- 10. refund target extras: maxResults override, fixed includedData ------------------------------
lst = tool("10 ListExecutions maxResults=2", "refund___ListExecutions", {"maxResults": 2}, quiet=True)
print([(e["name"], e["status"]) for e in lst["executions"]], "nextToken" in lst)
sm = tool("10 DescribeStateMachine (includedData fixed to ALL_DATA)", "refund___DescribeStateMachine", {}, quiet=True)
print("definition returned:", "definition" in sm)

(ROOT / "results" / "exercise_state.json").write_text(
    json.dumps({**cfg_out, "standardExecutionArn": ok["executionArn"], "usedTaskToken": task["taskToken"]}, indent=2)
)
print("\nlogged to", LOG, file=sys.stderr)
