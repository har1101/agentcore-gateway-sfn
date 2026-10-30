"""Create the demo resources: SQS queue, state machines, gateway service role and an IAM-authorized MCP gateway.

Idempotent enough to re-run: existing resources are reused. Targets are created separately (add_targets.py / console).
"""
import json
import time

from botocore.exceptions import ClientError

from common import PREFIX, REGION, ROOT, load_config, save_config, session

iam = session.client("iam")
sqs = session.client("sqs")
sfn = session.client("stepfunctions")
agc = session.client("bedrock-agentcore-control")
account = session.client("sts").get_caller_identity()["Account"]
cfg = load_config()
cfg.update(region=REGION, account=account)


def ensure_role(name: str, trust: dict, policy: dict) -> str:
    try:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
        print(f"created role {name}")
    except iam.exceptions.EntityAlreadyExistsException:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
    iam.put_role_policy(RoleName=name, PolicyName="inline", PolicyDocument=json.dumps(policy))
    return arn


# 1. SQS queue that receives task tokens from the callback workflow
queue_url = sqs.create_queue(QueueName="agc-demo-refund-review")["QueueUrl"]
queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
cfg.update(queueUrl=queue_url)

# 2. Execution role for the state machines
sm_arn = lambda name: f"arn:aws:states:{REGION}:{account}:stateMachine:{PREFIX}-{name}"  # noqa: E731
exec_role = ensure_role(
    f"{PREFIX}StatesExecutionRole",
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "states.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": account}},
            }
        ],
    },
    {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": "sqs:SendMessage", "Resource": queue_arn}],
    },
)

# 3. State machines
MACHINES = {
    "OrderFulfillment": ("order_fulfillment.asl.json", "STANDARD"),
    "RefundApproval": ("refund_approval.asl.json", "STANDARD"),
    "OrderLookupExpress": ("order_lookup_express.asl.json", "EXPRESS"),
    "OutOfScope": ("out_of_scope.asl.json", "STANDARD"),
}
cfg.setdefault("stateMachines", {})
for name, (file, sm_type) in MACHINES.items():
    definition = (ROOT / "statemachines" / file).read_text().replace("${QueueUrl}", queue_url)
    arn = sm_arn(name)
    for attempt in range(10):
        try:
            try:
                sfn.describe_state_machine(stateMachineArn=arn)
                sfn.update_state_machine(stateMachineArn=arn, definition=definition, roleArn=exec_role)
                print(f"updated state machine {name}")
            except sfn.exceptions.StateMachineDoesNotExist:
                sfn.create_state_machine(name=f"{PREFIX}-{name}", definition=definition, roleArn=exec_role, type=sm_type)
                print(f"created state machine {name} ({sm_type})")
            break
        except ClientError as e:  # new role not yet assumable
            if "AccessDenied" not in str(e) and "not authorized" not in str(e):
                raise
            time.sleep(5)
    cfg["stateMachines"][name] = arn

# 4. Gateway service role, scoped to the exposed state machines (OutOfScope deliberately excluded)
exposed = ["OrderFulfillment", "RefundApproval", "OrderLookupExpress"]
gw_name = "sfn-connector-demo"
gw_role = ensure_role(
    f"{PREFIX}GatewayToStepFunctions",
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{account}:gateway/{gw_name}-*"},
                },
            }
        ],
    },
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "StateMachineOperations",
                "Effect": "Allow",
                "Action": [
                    "states:StartExecution",
                    "states:StartSyncExecution",
                    "states:ListExecutions",
                    "states:DescribeStateMachine",
                ],
                "Resource": [sm_arn(n) for n in exposed],
            },
            {
                "Sid": "ExecutionOperations",
                "Effect": "Allow",
                "Action": ["states:DescribeExecution", "states:StopExecution", "states:GetExecutionHistory"],
                "Resource": [f"arn:aws:states:{REGION}:{account}:execution:{PREFIX}-{n}:*" for n in exposed],
            },
            {
                "Sid": "TaskTokenOperations",
                "Effect": "Allow",
                "Action": ["states:SendTaskSuccess", "states:SendTaskFailure", "states:SendTaskHeartbeat"],
                "Resource": "*",
            },
        ],
    },
)

# 5. Gateway (inbound auth = AWS_IAM / SigV4)
existing = [g for g in agc.list_gateways()["items"] if g["name"] == gw_name]
if existing:
    gw_id = existing[0]["gatewayId"]
else:
    for attempt in range(10):
        try:
            gw_id = agc.create_gateway(
                name=gw_name,
                description="AgentCore Gateway x Step Functions connector demo",
                protocolType="MCP",
                authorizerType="AWS_IAM",
                roleArn=gw_role,
            )["gatewayId"]
            break
        except ClientError as e:
            if "role" not in str(e).lower():
                raise
            time.sleep(5)
    print(f"created gateway {gw_id}")
while (gw := agc.get_gateway(gatewayIdentifier=gw_id))["status"] in ("CREATING", "UPDATING"):
    time.sleep(5)
print("gateway status:", gw["status"], gw.get("statusReasons"))
cfg.update(gatewayId=gw_id, gatewayUrl=gw["gatewayUrl"], gatewayRoleArn=gw_role)
save_config(cfg)
print(json.dumps(cfg, indent=2))
