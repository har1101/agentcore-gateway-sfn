# agentcore-gateway-sfn

Amazon Bedrock AgentCore Gateway の Step Functions コネクタ（`connectorId: "states"`）を試したときのコードです。
ステートマシン3つを Gateway のターゲットにして、10種類の操作を MCP ツールとして呼び出し、Strands Agents のエージェントからも使いました。

- 記事の下書き: [blog.md](blog.md)
- 検証の詳しいメモ: [BLOG_NOTES.md](BLOG_NOTES.md)（スクリーンショットと実行ログはアカウント情報を含むため、リポジトリには入れていません）

## 中身

| パス | 内容 |
|---|---|
| `statemachines/` | 注文処理（Standard）、返金審査（Standard、`.waitForTaskToken`）、注文照会（Express）、IAM 境界の確認用の ASL |
| `scripts/setup.py` | SQS、IAM ロール、ステートマシン、Gateway（AWS_IAM 認証）を作る |
| `scripts/add_targets.py` | `lookup` / `refund` / `tasks` / `misconfig` ターゲットを作る（`orders` はコンソールで作成） |
| `scripts/exercise.py` | 10種類の操作を MCP（SigV4 署名の JSON-RPC）で呼ぶ |
| `scripts/errors.py` | 固定したパラメータの上書き、Standard / Express の制約、IAM 境界などのエラーを確かめる |
| `scripts/agent_demo.py` | Strands Agents から Gateway につないで、注文処理と返金審査を任せる |
| `scripts/cleanup.py` | 作ったリソースを消す |
| `scripts/console_url.py` | CLI の認証情報からマネジメントコンソールのサインイン URL を作る |

## 使い方

```bash
export AWS_DEFAULT_REGION=ap-northeast-1
uv sync
uv run python scripts/setup.py         # 作成したリソースの ID は config.json に保存される
# コンソールで orders ターゲットを作る（BLOG_NOTES.md の手順）
uv run python scripts/add_targets.py
uv run python scripts/exercise.py
uv run python scripts/errors.py
uv run python scripts/agent_demo.py
uv run python scripts/cleanup.py
```

`aws login` の認証情報を boto3 で使うため、`boto3[crt]` を入れています。
エージェントのモデルは `jp.anthropic.claude-sonnet-4-6`（Amazon Bedrock）です。
