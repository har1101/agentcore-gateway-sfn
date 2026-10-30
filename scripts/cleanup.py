"""Delete everything created by setup.py / add_targets.py / the console (gateway targets, gateway, state machines,
SQS queue, IAM roles)."""
import time

from botocore.exceptions import ClientError

from common import CONFIG_PATH, PREFIX, load_config, session

cfg = load_config()
agc = session.client("bedrock-agentcore-control")
sfn = session.client("stepfunctions")
iam = session.client("iam")
sqs = session.client("sqs")

gw = cfg.get("gatewayId")
if gw:
    for t in agc.list_gateway_targets(gatewayIdentifier=gw)["items"]:
        agc.delete_gateway_target(gatewayIdentifier=gw, targetId=t["targetId"])
        print("deleted target", t["name"])
    while agc.list_gateway_targets(gatewayIdentifier=gw)["items"]:
        time.sleep(3)
    agc.delete_gateway(gatewayIdentifier=gw)
    print("deleted gateway", gw)

for name, arn in cfg.get("stateMachines", {}).items():
    try:
        sfn.delete_state_machine(stateMachineArn=arn)
        print("deleted state machine", name)
    except ClientError as e:
        print("skip", name, e)

if cfg.get("queueUrl"):
    sqs.delete_queue(QueueUrl=cfg["queueUrl"])
    print("deleted queue")

for role in (f"{PREFIX}GatewayToStepFunctions", f"{PREFIX}StatesExecutionRole"):
    try:
        iam.delete_role_policy(RoleName=role, PolicyName="inline")
        iam.delete_role(RoleName=role)
        print("deleted role", role)
    except iam.exceptions.NoSuchEntityException:
        pass

CONFIG_PATH.unlink(missing_ok=True)
