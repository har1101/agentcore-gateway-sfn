"""Print an AWS console sign-in URL from the current CLI credentials (federation endpoint)."""
import json
import sys
import urllib.parse
import urllib.request

import boto3

destination = sys.argv[1] if len(sys.argv) > 1 else "https://ap-northeast-1.console.aws.amazon.com/"
creds = boto3.Session().get_credentials().get_frozen_credentials()
session = json.dumps(
    {"sessionId": creds.access_key, "sessionKey": creds.secret_key, "sessionToken": creds.token}
)
token_url = "https://signin.aws.amazon.com/federation?" + urllib.parse.urlencode(
    {"Action": "getSigninToken", "Session": session}
)
signin_token = json.load(urllib.request.urlopen(token_url))["SigninToken"]
print(
    "https://signin.aws.amazon.com/federation?"
    + urllib.parse.urlencode(
        {
            "Action": "login",
            "Issuer": "agentcore-gateway-sfn",
            "Destination": destination,
            "SigninToken": signin_token,
        }
    )
)
