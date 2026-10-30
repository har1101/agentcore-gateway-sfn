こんにちは、ふくちです。

AgentCore GatewayのターゲットにStep Functionsが選べるようになったので、一通り触ってみました。

https://aws.amazon.com/about-aws/whats-new/2026/10/step-functions-workflows-ai-agent-tools/

https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-connector-step-functions.html

# 何ができるのか
既存のステートマシンを、そのままMCPツールとしてエージェントに渡せます。これまでもAPI GatewayやLambdaはGatewayのターゲットにできたので、その対象がStep Functionsにも広がった形ですね。

エージェントが決めるのは、いつ・どんな入力（input）で動かすかだけです。それ以降の処理（どのステップをどの順で実行するかなど）は、ステートマシン側の定義次第です。

ツールにできるのはStep FunctionsのAPI10個です(今のところ)。これらを分類すると3種類くらいになりそうです。

| 種類 | 操作 |
|---|---|
| ステートマシンを固定するもの | StartExecution, StartSyncExecution, ListExecutions, DescribeStateMachine |
| 実行ARNを受け取るもの | DescribeExecution, GetExecutionHistory, StopExecution |
| タスクトークンを受け取るもの | SendTaskSuccess, SendTaskFailure, SendTaskHeartbeat |

ここでいう固定(ドキュメントではpinと記載されているもの)は、ターゲットを作るときに、操作ごとに対象のステートマシンのARNを決めておくことです。設定では各操作の `parameterValues.stateMachineArn` にARNを書きます。
例えばStartExecutionを注文処理のステートマシンに固定すると、エージェントから見えるツールの引数は `input` だけになり、呼べば必ずそのステートマシンが起動します。エージェントが別のARNを無理やり渡しても `cannot set parameter(s): [/stateMachineArn]` で弾かれます。

一方、実行ARNやタスクトークンを受け取る6つは、対象をエージェントが引数で渡すので固定できません。どの実行まで触れるかはGatewayのサービスロールのIAMポリシー次第です。

タスクトークンは、ステートマシンが途中で止まって外からの返事を待つときに、Step Functionsが発行する文字列です。返事をする側はこのトークンを付けてSendTask系の操作を呼びます。具体的な使い方は後の返金審査のところで書きます。

# 試した構成
動作確認用に、次の3つのステートマシンを用意しました。どれも簡単なもので、外部のサービスを呼ぶのは返金審査のSQSへの送信だけです。

| ステートマシン | タイプ | 処理 |
|---|---|---|
| 注文処理 | Standard | 注文金額をチェックし（10万円を超えたら失敗）、在庫引当 → 出荷待ち（指定した秒数だけ待つ）→ 出荷の順に進んで、追跡番号を返す |
| 返金審査 | Standard | 返金申請を受けると、審査依頼をSQSに送って止まる。エージェントの判断が返ってきたら、承認なら返金し、承認でなければ失敗で終わる |
| 注文照会 | Express | 注文IDを受け取り、その注文のステータスをその場で返す |

Gatewayには次の4つのターゲットを登録しました。ツール名は `<ターゲット名>___<操作名>`（例: `orders___StartExecution`）になります。

| ターゲット | 操作 | 固定先 |
|---|---|---|
| orders | StartExecution, ListExecutions, DescribeStateMachine, DescribeExecution, GetExecutionHistory, StopExecution | 注文処理 |
| lookup | StartSyncExecution | 注文照会 |
| refund | StartExecution, ListExecutions, DescribeStateMachine | 返金審査 |
| tasks | SendTaskSuccess, SendTaskFailure, SendTaskHeartbeat | なし |

エージェントはStrands Agentsで作り、SigV4（IAM認証）でGatewayにつないでいます。ordersターゲットだけコンソールで作り、残りはboto3で作りました。

# コンソールで作る
AgentCore Gatewayのターゲット追加画面で、ターゲットタイプに「コネクター」、その下の一覧で「Step Functions」を選びます。
扱いとしてはナレッジベースと同じなんですね。

![image.png](https://qiita-image-store.s3.ap-northeast-1.amazonaws.com/0/3616210/f56d6d74-dd5d-417c-a45c-d71600aa59eb.png)

あとはステートマシンを1つ選んで、そのステートマシンに対して実行して良い操作を選ぶだけです。
最初は10個全部の操作が選択されており、Standardのステートマシンを選ぶとStartSyncExecutionはExpress専用という警告が出ます。

![image.png](https://qiita-image-store.s3.ap-northeast-1.amazonaws.com/0/3616210/924a3840-2b44-4f74-bc1f-2ff1c671b8e0.png)

StartSyncExecutionを外せばOKですね。
![image.png](https://qiita-image-store.s3.ap-northeast-1.amazonaws.com/0/3616210/947a59ca-4959-4350-98ff-864f83a5f50c.png)

ちなみにコンソールでは、1つのターゲットで選べるステートマシンは1つだけです。StartSyncExecutionだけExpressに向けるような操作ごとの固定は、APIから作る必要があります。

# エージェントから呼んでみる
## 注文処理
「注文D-3001（8,000円）を出荷して、追跡番号を教えて」と頼むと、エージェントは `orders___StartExecution` で注文処理を起動し、`orders___DescribeExecution` で終わるまで状態を確かめてから、追跡番号を返してくれました。
出荷待ちを600秒にした注文で「止めて」と頼むと、`orders___StopExecution` で止めてくれました。

## 返金審査とタスクトークン
返金審査では、ワークフローが途中で止まってエージェントの判断を待ちます。この待ち合わせに使うのがタスクトークンです。

設定するのは、ステートマシンの定義（ASL）の中の、審査依頼をSQSに送るTask状態です。今回は次のように書きました。

```json
"RequestAgentReview": {
  "Type": "Task",
  "Resource": "arn:aws:states:::sqs:sendMessage.waitForTaskToken",
  "Arguments": {
    "QueueUrl": "<SQSキューのURL>",
    "MessageBody": {
      "taskToken": "{% $states.context.Task.Token %}",
      "executionArn": "{% $states.context.Execution.Id %}",
      "request": "{% $states.input %}"
    }
  },
  "HeartbeatSeconds": 120,
  "TimeoutSeconds": 3600,
  "Output": "{% $merge([$states.input, {'review': $states.result}]) %}",
  "Next": "IsApproved"
}
```

ポイントは2つです。

- `Resource` にはSQSにメッセージを送る `arn:aws:states:::sqs:sendMessage` の末尾に `.waitForTaskToken` を付けたものを書きます。こうすると、Step FunctionsはSQSにメッセージを送ったあと、返事が来るまでこの状態で実行を止めます。
- 送るメッセージ（`MessageBody`）に、`$states.context.Task.Token` でタスクトークンを入れます。返事をする側はこのトークンでどの実行のどのタスクへの返事かを伝えます。

実際の流れはこうなります。

1. エージェントが `refund___StartExecution` で返金審査を起動する
2. ワークフローがタスクトークンと申請内容をSQSに送って止まる
3. エージェントがSQSを読む自作のツールでメッセージを受け取り、申請内容を見て判断する
4. エージェントが判断結果を `output`（例: `{"approved": true, "comment": "..."}`）に入れて `tasks___SendTaskSuccess` を呼ぶ。ワークフローが再開し、`approved` がtrueなら返金し、falseなら失敗で終わる
5. 申請そのものを受け付けられないときは、`tasks___SendTaskFailure` を呼ぶ。そのタスクが失敗になり、実行も失敗で終わる

審査に時間がかかるときは、`tasks___SendTaskHeartbeat` で作業中だと伝えます。上の定義では `HeartbeatSeconds` を120にしているので、120秒のあいだ返事もハートビートもなければタイムアウトします。

トークンは1回しか使えず、使い終わったものをもう一度送ると `TaskTimedOut` になりました。また、SendTask系はIAMで対象の実行を絞れないので、権限さえあれば、トークンを持っている人は誰でもそのタスクを完了できます。トークンは秘密情報として扱いましょう。

なお、コネクタはタスクトークンをエージェントに届けてくれません。今回のSQSのように、受け渡しの経路は自分で用意する必要があります。

# ハマりどころ
## 日本語の文字化け
ツールの引数に日本語をそのまま入れると、Step Functionsに届いた時点で `?????` になりました。同じ内容をboto3から直接SendTaskFailureで送ると正しく入るので、コネクタ側の問題っぽいです。

`input` と `output` はJSON文字列なので、`json.dumps(..., ensure_ascii=True)` で `\uXXXX` にしてから渡せば大丈夫でした。ただSendTaskFailureの `error` と `cause` はただの文字列なので逃げ道がなく、英語で書くしかなさそうです。

## SendTask系だけのターゲット
ドキュメントにはSendTask系は `parameterValues` なしで追加すると書かれていますが、名前だけを並べると `Connector configurations must not be empty.` で弾かれます。空の `parameterValues` を付ければ通ります。コンソールで作ったターゲットにも空のものが付いていました。

```python
agc.create_gateway_target(
    gatewayIdentifier=GATEWAY_ID,
    name="tasks",
    targetConfiguration={"mcp": {"connector": {
        "source": {"connectorId": "states", "version": "1.0.0"},
        "configurations": [
            {"name": "SendTaskSuccess", "parameterValues": {}},
            {"name": "SendTaskFailure", "parameterValues": {}},
            {"name": "SendTaskHeartbeat", "parameterValues": {}},
        ],
    }}},
    credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
)
```

## AWS CLIの対応状況
手元のAWS CLI（2.34.45）の `create-gateway-target` はconnectorの指定に対応していなかったので、boto3で作りました。

## 誤設定の検出
StandardのステートマシンにStartSyncExecutionを固定した場合も、固定し忘れた場合も、サービスロールで許可していないステートマシンを固定した場合も、ターゲットはReadyになり、呼んだときに初めてエラーになります。
