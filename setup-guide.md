# Self-Healing Lambda — Manual AWS Setup Guide

This guide walks you through creating every AWS resource needed to deploy
the self-healing Lambda that triggers a rolling restart of EKS Deployments
on Grafana alerts.

## Architecture

```
Grafana Alert (firing, namespace + deployment labels)
    │
    ▼ HTTPS webhook with Bearer token
API Gateway (HTTPS endpoint)
    │
    ▼ Invoke
AWS Lambda (app.handler)
    ├── Validate HTTP method (POST only)
    ├── Validate Bearer token (constant-time comparison)
    ├── Validate namespace matches ALLOWED_NAMESPACE
    ├── Parse Grafana payload, extract deployment name
    ├── Generate EKS bearer token (SigV4 over STS)
    └── PATCH Deployment annotation (kubectl.kubernetes.io/restartedAt)
          → Kubernetes recreates all pods (rolling update)
```

## Prerequisites

- AWS CLI configured with permissions for IAM, Lambda, API Gateway, and EKS
- `kubectl` configured for your EKS cluster
- An EKS cluster with workloads running
- Grafana alerting already configured (you'll set the webhook URL after
  creating the API Gateway)

## Step 1: Create the Lambda Execution IAM Role

### 1a. Create the role with a trust policy

```bash
cat > trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {"Service": "lambda.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

aws iam create-role \
  --role-name self-healing-lambda-role \
  --assume-role-policy-document file://trust-policy.json
```

Note the role ARN — you'll need it in every step that follows:

```bash
ROLE_ARN=$(aws iam get-role \
  --role-name self-healing-lambda-role \
  --query 'Role.Arn' \
  --output text)
echo $ROLE_ARN
```

### 1b. Attach the basic execution policy (CloudWatch Logs)

```bash
aws iam attach-role-policy \
  --role-name self-healing-lambda-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
```

### 1c. Add inline policy for STS (EKS token generation)

The Lambda signs an STS GetCallerIdentity request to produce the EKS
bearer token. Grant that single permission:

```bash
cat > sts-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "sts:GetCallerIdentity",
      "Resource": "*"
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name self-healing-lambda-role \
  --policy-name sts-getcalleridentity \
  --policy-document file://sts-policy.json
```

## Step 2: Grant EKS Access to the Lambda Role

### Check your cluster's authentication mode

```bash
aws eks describe-cluster \
  --name <EKS_CLUSTER_NAME> \
  --query 'cluster.accessConfig.authenticationMode' \
  --output text
```

- `API` or `API_AND_CONFIG_MAP` → use **Option A** (EKS Access Entries)
- `CONFIG_MAP` → use **Option B** (aws-auth ConfigMap)

### Option A: EKS Access Entry (recommended)

Create an access entry that maps the Lambda role to a Kubernetes group.
The group is what the RBAC RoleBinding binds to (Step 3):

```bash
aws eks create-access-entry \
  --cluster-name <EKS_CLUSTER_NAME> \
  --principal-arn $ROLE_ARN \
  --kubernetes-groups self-healing
```

### Option B: aws-auth ConfigMap (legacy)

```bash
kubectl edit configmap aws-auth -n kube-system
```

Add this entry under `data.mapRoles`:

```yaml
- rolearn: <LAMBDA_ROLE_ARN>
  username: self-healing
  groups:
    - self-healing
```

## Step 3: Create Kubernetes RBAC

The Lambda only needs to `patch` Deployments in one namespace. Save this
YAML as `rbac.yaml` — replace `<ALLOWED_NAMESPACE>` with your target
namespace (e.g. `production`):

```yaml
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: self-healing-restart
  namespace: <ALLOWED_NAMESPACE>
rules:
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["patch", "get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: self-healing-restart
  namespace: <ALLOWED_NAMESPACE>
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: self-healing-restart
subjects:
  - kind: Group
    name: self-healing
    apiGroup: rbac.authorization.k8s.io
```

Apply it:

```bash
kubectl apply -f rbac.yaml
```

> The `get` verb is optional but useful for troubleshooting with
> `kubectl get deployment`. The only strictly required verb is `patch`.

## Step 4: Create the Lambda Function

### 4a. Package the function

The Lambda uses only the Python standard library and boto3 (both already
in the Lambda runtime), so there are no dependencies to install. Zip just
the handler:

```bash
cd self-healing
zip function.zip app.py
```

### 4b. Gather EKS cluster details

```bash
EKS_CLUSTER_NAME="<your-cluster-name>"

CLUSTER_ENDPOINT=$(aws eks describe-cluster \
  --name $EKS_CLUSTER_NAME \
  --query 'cluster.endpoint' \
  --output text)

CA_CERT=$(aws eks describe-cluster \
  --name $EKS_CLUSTER_NAME \
  --query 'cluster.certificateAuthority.data' \
  --output text)

AWS_REGION=$(aws configure get region)
```

### 4c. Create the Lambda

Generate a strong bearer token for Grafana authentication:

```bash
WEBHOOK_TOKEN=$(openssl rand -hex 32)
echo "Save this token: $WEBHOOK_TOKEN"
```

Create the function with all environment variables:

```bash
aws lambda create-function \
  --function-name self-healing-restart \
  --runtime python3.12 \
  --handler app.handler \
  --role $ROLE_ARN \
  --zip-file fileb://function.zip \
  --timeout 30 \
  --memory-size 256 \
  --environment "Variables={\
WEBHOOK_BEARER_TOKEN=$WEBHOOK_TOKEN,\
ALLOWED_NAMESPACE=<your-namespace>,\
EKS_CLUSTER_NAME=$EKS_CLUSTER_NAME,\
EKS_CLUSTER_ENDPOINT=$CLUSTER_ENDPOINT,\
EKS_CLUSTER_CA_CERTIFICATE_BASE64=$CA_CERT,\
AWS_REGION=$AWS_REGION}"
```

### Environment Variables Reference

| Variable | Required | Example | Purpose |
| --- | --- | --- | --- |
| `WEBHOOK_BEARER_TOKEN` | Yes | (random hex string) | Shared secret validated against Grafana's `Authorization` header |
| `ALLOWED_NAMESPACE` | Yes | `production` | Only this namespace can be restarted |
| `EKS_CLUSTER_NAME` | Yes | `my-cluster` | Used to sign the EKS bearer token (x-k8s-aws-id header) |
| `EKS_CLUSTER_ENDPOINT` | Yes | `https://...eks.amazonaws.com` | Kubernetes API URL |
| `EKS_CLUSTER_CA_CERTIFICATE_BASE64` | Yes | (base64 string) | Cluster CA certificate for TLS verification |
| `AWS_REGION` | Yes | `us-east-1` | Region for STS and the EKS cluster |

## Step 5: Create the API Gateway (HTTP API)

The API Gateway provides the HTTPS endpoint that Grafana calls.

### Console approach (recommended for first-time setup)

1. Open the **API Gateway** console in AWS
2. Click **Create API** → **HTTP API** → **Build**
3. **Integrations**: Add integration → Lambda → select `self-healing-restart`
4. **Routes**: Add route → Method `POST`, Path `/`
5. **Stage**: auto-deploy with stage name `$default`
6. Click **Create**
7. Copy the **Invoke URL** — this is what Grafana will call

### CLI approach

```bash
LAMBDA_ARN=$(aws lambda get-function \
  --function-name self-healing-restart \
  --query 'Configuration.FunctionArn' \
  --output text)

API_ID=$(aws apigatewayv2 create-api \
  --name self-healing-webhook \
  --protocol-type HTTP \
  --target $LAMBDA_ARN \
  --query 'ApiId' \
  --output text)

echo "Webhook URL: https://$API_ID.execute-api.$AWS_REGION.amazonaws.com/"
```

If using the CLI, grant API Gateway permission to invoke the Lambda:

```bash
aws lambda add-permission \
  --function-name self-healing-restart \
  --statement-id apigateway-invoke \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:$AWS_REGION:$(aws sts get-caller-identity --query Account --output text):$API_ID/*/*/POST/"
```

## Step 6: Network Access to the EKS API

The Lambda must reach the EKS API endpoint over HTTPS (port 443).

- **Public endpoint** (default): The Lambda can reach it directly. No
  extra configuration needed.
- **Private endpoint**: The Lambda needs VPC configuration with a route
  to the cluster's private subnets, and the EKS API server VPC endpoint
  must be accessible. Configure this in the Lambda's VPC settings.

Check your endpoint type:

```bash
aws eks describe-cluster \
  --name $EKS_CLUSTER_NAME \
  --query 'cluster.resourcesVpcConfig.endpointPublicAccess' \
  --output text
```

## Step 7: Grafana Webhook Reference

Grafana is already configured, but for reference, the webhook should point
to the API Gateway URL with these settings:

- **URL**: `https://<api-id>.execute-api.<region>.amazonaws.com/`
- **HTTP Method**: `POST`
- **Authorization header**: `Bearer <WEBHOOK_BEARER_TOKEN>`

The alert payload must include `namespace` and `deployment` labels:

```json
{
  "status": "firing",
  "alerts": [
    {
      "status": "firing",
      "labels": {
        "alertname": "HighErrorRate",
        "namespace": "production",
        "deployment": "auth-service"
      }
    }
  ]
}
```

In Grafana, add these as custom labels in the alert rule or notification
policy so they're included in the webhook payload.

## Step 8: Verification

### Test the webhook with curl

```bash
WEBHOOK_URL="https://<api-id>.execute-api.<region>.amazonaws.com/"

curl -s -X POST \
  -H "Authorization: Bearer $WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "status": "firing",
    "alerts": [{
      "status": "firing",
      "labels": {
        "alertname": "HighErrorRate",
        "namespace": "production",
        "deployment": "auth-service"
      }
    }]
  }' \
  $WEBHOOK_URL
```

Expected response:

```json
{"result": "deployment restart requested", "deployment": "auth-service"}
```

### Check CloudWatch Logs

```bash
aws logs tail /aws/lambda/self-healing-restart --follow
```

### Verify the rolling restart

```bash
kubectl rollout status deployment/auth-service -n production
```

You should see pods being recreated.

## Troubleshooting

| Response | Likely Cause | Fix |
| --- | --- | --- |
| `401 Unauthorized` | Bearer token mismatch | Verify `WEBHOOK_BEARER_TOKEN` matches Grafana |
| `400` with namespace error | Wrong namespace in alert | Add the correct `namespace` label in Grafana |
| `400` with deployment error | Missing or invalid deployment label | Add the `deployment` label in Grafana |
| `502` authorization denied | EKS access entry or RBAC missing | Recheck Steps 2 and 3 |
| `502` deployment restart failed | Network or EKS API error | Check network access (Step 6) and CloudWatch logs |
| `500` unexpected failure | Unhandled exception | Check CloudWatch logs for the stack trace |
| Timeout | EKS API unreachable | If endpoint is private, configure Lambda VPC access |
