"""Strands Agents demo: an operations/support agent that uses Step Functions workflows through AgentCore Gateway.

- MCP over streamable HTTP, SigV4-signed (gateway inbound auth = AWS_IAM)
- Local tool `fetch_review_tasks` plays the "agent application" that receives task tokens from SQS
- Hook escapes non-ASCII characters in JSON payloads (the connector forwards raw non-ASCII as '?')

Writes a Markdown transcript to results/agent_transcript.md.
"""
import json
import re
import sys
import time

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from strands import Agent, tool
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

from common import REGION, ROOT, load_config, session

cfg = load_config()
sqs = session.client("sqs")
MODEL_ID = "jp.anthropic.claude-sonnet-4-6"
TRANSCRIPT = ROOT / "results" / "agent_transcript.md"
lines: list[str] = []


class SigV4HttpxAuth(httpx.Auth):
    """Sign every MCP HTTP request with the current AWS credentials (service: bedrock-agentcore)."""

    requires_request_body = True

    def auth_flow(self, request: httpx.Request):
        headers = {k: v for k, v in request.headers.items() if k.lower() not in ("connection", "accept-encoding")}
        aws_req = AWSRequest(method=request.method, url=str(request.url), data=request.content, headers=headers)
        SigV4Auth(session.get_credentials().get_frozen_credentials(), "bedrock-agentcore", REGION).add_auth(aws_req)
        request.headers.update(dict(aws_req.headers))
        yield request


JSON_STRING_ARGS = {"StartExecution": "input", "StartSyncExecution": "input", "SendTaskSuccess": "output"}


class GatewayToolHooks(HookProvider):
    """Log tool calls and re-serialize JSON payload arguments with \\uXXXX escapes (ASCII only)."""

    def register_hooks(self, registry: HookRegistry, **_) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before)
        registry.add_callback(AfterToolCallEvent, self.after)

    def before(self, event: BeforeToolCallEvent) -> None:
        name, args = event.tool_use["name"], event.tool_use["input"]
        key = JSON_STRING_ARGS.get(name.rsplit("___", 1)[-1])
        if key and isinstance(args.get(key), str):
            try:
                args[key] = json.dumps(json.loads(args[key]), ensure_ascii=True)
            except json.JSONDecodeError:
                pass
        shown = {k: (v[:40] + "…" if k == "taskToken" else v) for k, v in args.items()}
        log(f"- 🔧 `{name}` `{json.dumps(shown, ensure_ascii=False)}`")

    def after(self, event: AfterToolCallEvent) -> None:
        content = event.result.get("content", [])
        text = " ".join(c.get("text", "") for c in content if "text" in c)
        status = event.result.get("status")
        log(f"  - ↩︎ {status}: `{text[:300]}`")


def log(s: str) -> None:
    print(s, flush=True)
    lines.append(s)


@tool
def fetch_review_tasks() -> list[dict]:
    """返金審査キュー(SQS)から審査待ちタスクを取得する。各タスクは taskToken, executionArn, request(申請内容) を持つ。
    取得したタスクは必ず SendTaskSuccess か SendTaskFailure で完了させること。"""
    tasks = []
    for _ in range(3):
        msgs = sqs.receive_message(QueueUrl=cfg["queueUrl"], WaitTimeSeconds=3, MaxNumberOfMessages=10).get("Messages", [])
        for m in msgs:
            tasks.append(json.loads(m["Body"]))
            sqs.delete_message(QueueUrl=cfg["queueUrl"], ReceiptHandle=m["ReceiptHandle"])
        if tasks:
            break
    return tasks


@tool
def sleep_seconds(seconds: int) -> str:
    """指定秒数待つ。ワークフローの完了をポーリングする間隔に使う (最大30秒)。"""
    time.sleep(min(seconds, 30))
    return f"slept {min(seconds, 30)}s"


SYSTEM_PROMPT = """あなたは EC サイトの注文・返金オペレーションを担当するエージェントです。
業務処理は必ず Step Functions のワークフロー (ツール) を通して実行し、自分で結果を推測しないでください。

ツールの使い分け:
- lookup___StartSyncExecution: 注文ステータスの即時照会 (Express)。input は {"orderId": "..."}。
- orders___*: 注文処理ワークフロー。StartExecution の input は {"orderId": string, "amount": number, "waitSeconds": number(省略可)}。
  起動後は DescribeExecution (includedData=ALL_DATA) で完了まで確認する。RUNNING の間は sleep_seconds で待ってから再確認する。
  orders___DescribeExecution / GetExecutionHistory / StopExecution は executionArn を指定すれば返金ワークフローの実行にも使える。
- refund___*: 返金ワークフロー。StartExecution の input は {"orderId": string, "amount": number, "reason": string}。
  name には "refund-<orderId>" を指定して二重申請を防ぐ。
- 返金審査: fetch_review_tasks で審査待ちタスクを取得し、ポリシーに従って
  承認なら tasks___SendTaskSuccess (output は {"approved": true/false, "comment": "..."})、
  ポリシー違反で処理不能なら tasks___SendTaskFailure (error/cause は英語の ASCII 文字のみ) を呼ぶ。
  審査に時間がかかる場合は tasks___SendTaskHeartbeat を呼ぶ。
返金ポリシー: 商品破損・誤配送は承認。サイズ違いは approved=false (交換で対応)。自己都合は SendTaskFailure で却下。
返金審査 (fetch_review_tasks 以降) は、ユーザーから審査を指示されたときだけ行うこと。

回答は日本語で簡潔に。"""

PROMPTS = [
    "注文 1002 と 1004 の今のステータスを教えて。",
    "注文 D-3001 (金額 8,000円) の出荷処理を開始して、完了したら追跡番号を教えて。出荷待ちは 3 秒で。",
    "注文 D-3002 (金額 150,000円) も出荷処理して。",
    "注文 D-3003 (金額 3,000円, 出荷待ち 600 秒) の出荷処理を開始して。",
    "さっきの D-3003 はお客様都合でキャンセルになった。処理を止めて、止まったことを確認して。",
    "注文処理ワークフローで失敗した実行や中断された実行はある? それぞれ原因を教えて。",
    "次の返金申請を受け付けて: (1) 注文 R-11 5,000円 理由「商品が割れていた」 (2) 注文 R-12 12,000円 理由「イメージと違ったので」 (3) 注文 R-13 4,000円 理由「サイズが合わない」",
    "返金審査キューにあるタスクをポリシーに従って処理して。",
    "さっきの返金申請 3 件の最終結果をまとめて。",
]


def main() -> None:
    gateway = MCPClient(
        url=cfg["gatewayUrl"],
        auth_provider=SigV4HttpxAuth(),
        tool_filters={"rejected": [re.compile(r"^misconfig___")]},
        application_name="sfn-connector-demo-agent",
    )
    with gateway:
        mcp_tools = gateway.list_tools_sync()
        log(f"# Agent transcript ({MODEL_ID})\n\nMCP tools: {', '.join(sorted(t.tool_name for t in mcp_tools))}\n")
        agent = Agent(
            model=BedrockModel(model_id=MODEL_ID, boto_session=session),
            system_prompt=SYSTEM_PROMPT,
            tools=[*mcp_tools, fetch_review_tasks, sleep_seconds],
            hooks=[GatewayToolHooks()],
            callback_handler=None,
        )
        # Args: indexes into PROMPTS or free-text prompts. Partial runs get their own transcript file.
        prompts = [PROMPTS[int(a)] if a.isdigit() else a for a in sys.argv[1:]] or PROMPTS
        for prompt in prompts:
            log(f"\n## 👤 {prompt}\n")
            result = agent(prompt)
            log(f"\n**🤖** {str(result).strip()}\n")
    out = TRANSCRIPT if len(sys.argv) == 1 else TRANSCRIPT.with_name(f"agent_transcript_{time.strftime('%Y%m%dT%H%M%S')}.md")
    out.write_text("\n".join(lines) + "\n")
    print(f"transcript: {out}")


if __name__ == "__main__":
    main()
