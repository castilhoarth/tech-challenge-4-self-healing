"""Local smoke test — simulates Grafana webhooks without AWS resources.

Run with:
    python test_local.py

By default this mocks the EKS API call so you can see the handler logic
work end-to-end without any AWS resources. To test against a real EKS
cluster, set the environment variables (see setup-guide.md) and pass --live.
"""

import json
import os
import sys

# --- Minimal environment so the handler can run ---------------------------

os.environ.setdefault("WEBHOOK_BEARER_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_NAMESPACE", "production")
os.environ.setdefault("EKS_CLUSTER_NAME", "test-cluster")
os.environ.setdefault("EKS_CLUSTER_ENDPOINT", "https://test-cluster.eks.amazonaws.com")
os.environ.setdefault("EKS_CLUSTER_CA_CERTIFICATE_BASE64", "dGVzdC1jYS1jZXJ0")  # base64("test-ca-cert")
os.environ.setdefault("AWS_REGION", "us-east-1")

sys.path.insert(0, os.path.dirname(__file__))

from app import handler

LIVE = "--live" in sys.argv

if not LIVE:
    # Mock restart_deployment so we don't need real AWS/EKS
    from unittest.mock import patch

    def fake_restart(namespace, deployment):
        print(f"   [MOCK] Would PATCH deployment '{deployment}' in namespace '{namespace}'")

    handler_module = sys.modules["app"]
    handler_module.restart_deployment = fake_restart


# --- Simulated Grafana webhook events -------------------------------------

def grafana_event(status="firing", namespace="production", deployment="auth-service",
                  token="Bearer test-token"):
    return {
        "headers": {"Authorization": token},
        "body": json.dumps({
            "status": status,
            "alerts": [
                {
                    "status": status,
                    "labels": {
                        "alertname": "HighErrorRate",
                        "namespace": namespace,
                        "deployment": deployment,
                    },
                }
            ],
        }),
    }


def run(label, event):
    print(f"\n{'─' * 60}")
    print(f"  {label}")
    print(f"{'─' * 60}")
    response = handler(event, None)
    print(f"  Status: {response['statusCode']}")
    print(f"  Body:   {json.loads(response['body'])}")
    return response


# --- Scenarios ------------------------------------------------------------

print("=" * 60)
print("  SELF-HEALING LAMBDA — LOCAL SMOKE TEST")
print(f"  Mode: {'LIVE (real EKS)' if LIVE else 'MOCKED (no AWS)'}")
print("=" * 60)

# 1. Happy path — valid firing alert
run("1. Valid firing alert (happy path)",
    grafana_event())

# 2. Resolved alert — should be ignored
run("2. Resolved alert (ignored)",
    grafana_event(status="resolved"))

# 3. Wrong token — should be rejected
run("3. Wrong bearer token (401)",
    grafana_event(token="Bearer wrong-token"))

# 4. Wrong namespace — should be rejected
run("4. Wrong namespace (400)",
    grafana_event(namespace="kube-system"))

# 5. Invalid deployment name — should be rejected
run("5. Invalid deployment name (400)",
    grafana_event(deployment="../etc"))

# 6. Missing deployment label
run("6. Missing deployment label (400)",
    {
        "headers": {"Authorization": "Bearer test-token"},
        "body": json.dumps({
            "status": "firing",
            "alerts": [{
                "status": "firing",
                "labels": {
                    "alertname": "HighErrorRate",
                    "namespace": "production",
                },
            }],
        }),
    })

# 7. Malformed JSON
run("7. Malformed JSON body (400)",
    {"headers": {"Authorization": "Bearer test-token"}, "body": "{not-json"})

# 8. Non-POST method
run("8. Non-POST method (405)",
    {**grafana_event(), "requestContext": {"http": {"method": "GET"}}})

print("\n" + "=" * 60)
print("  Done. All scenarios exercised.")
print("=" * 60)
