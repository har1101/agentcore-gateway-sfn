"""Create (or update) the script-managed Step Functions connector targets.

The `orders` target (OrderFulfillment, 6 operations) is created in the console; see screenshots/2x_*.png.
"""
import json
import time

from common import load_config, save_config, session

agc = session.client("bedrock-agentcore-control")
cfg = load_config()
sm = cfg["stateMachines"]
SOURCE = {"connectorId": "states", "version": "1.0.0"}


def pin(arn: str) -> dict:
    return {"stateMachineArn": arn}


TARGETS = {
    # Express workflow, synchronous request/response
    "lookup": [
        {
            "name": "StartSyncExecution",
            "description": "注文IDから注文ステータスを即時に照会する。input は {\"orderId\": \"1001\"} の形式の JSON 文字列。",
            "parameterValues": pin(sm["OrderLookupExpress"]),
        },
    ],
    # Standard callback workflow; parameterOverrides expose optional fields to the agent
    "refund": [
        {
            "name": "StartExecution",
            "description": "返金申請ワークフローを開始する。input は {\"orderId\": string, \"amount\": number, \"reason\": string}。name に冪等キーを指定できる。",
            "parameterValues": pin(sm["RefundApproval"]),
            "parameterOverrides": [
                {"path": "$.name", "visible": True, "description": "実行名 (冪等キー)。同じ名前で再実行すると重複起動を防げる。"}
            ],
        },
        {
            "name": "ListExecutions",
            "parameterValues": pin(sm["RefundApproval"]),
            "parameterOverrides": [{"path": "$.maxResults", "visible": True}],
        },
        {
            "name": "DescribeStateMachine",
            # Fix includedData=ALL_DATA and hide it from the agent
            "parameterValues": {**pin(sm["RefundApproval"]), "includedData": "ALL_DATA"},
            "parameterOverrides": [{"path": "$.includedData", "visible": False}],
        },
    ],
    # Task-token operations: not scoped to a state machine, so one separate target per gateway.
    # NOTE: a target whose entries all lack `parameterValues` is rejected with
    # "Connector configurations must not be empty", so pass an empty object (the console does the same).
    "tasks": [
        {"name": "SendTaskSuccess", "parameterValues": {}},
        {"name": "SendTaskFailure", "parameterValues": {}},
        {"name": "SendTaskHeartbeat", "parameterValues": {}},
    ],
    # Deliberately wrong configuration to observe the Standard/Express and missing-pin errors
    "misconfig": [
        {"name": "StartSyncExecution", "parameterValues": pin(sm["OrderFulfillment"])},  # Standard!
        {"name": "ListExecutions", "parameterValues": pin(sm["OrderLookupExpress"])},  # Express!
        {"name": "DescribeStateMachine"},  # no pin
        {"name": "StartExecution", "parameterValues": pin(sm["OutOfScope"])},  # not allowed by the service role
    ],
}

existing = {t["name"]: t["targetId"] for t in agc.list_gateway_targets(gatewayIdentifier=cfg["gatewayId"])["items"]}
cfg["targets"] = dict(existing)
for name, configurations in TARGETS.items():
    kwargs = dict(
        gatewayIdentifier=cfg["gatewayId"],
        name=name,
        targetConfiguration={"mcp": {"connector": {"source": SOURCE, "configurations": configurations}}},
        credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    )
    if name in existing:
        resp = agc.update_gateway_target(targetId=existing[name], **kwargs)
    else:
        resp = agc.create_gateway_target(**kwargs)
    cfg["targets"][name] = resp["targetId"]
    print(f"{name}: {resp['targetId']} {resp['status']}")

for name, tid in cfg["targets"].items():
    while (t := agc.get_gateway_target(gatewayIdentifier=cfg["gatewayId"], targetId=tid))["status"] in (
        "CREATING",
        "UPDATING",
    ):
        time.sleep(3)
    print(f"{name}: {t['status']} {json.dumps(t.get('statusReasons', []), ensure_ascii=False)}")
save_config(cfg)
