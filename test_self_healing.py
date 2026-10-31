"""Unit and integration tests for the self-healing Lambda."""

import base64
import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from app import (
    KubernetesApiError,
    _eks_bearer_token,
    _get_firing_target,
    _is_valid_deployment_name,
    _kubernetes_request,
    _parse_payload,
    _response,
    handler,
    restart_deployment,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def configured_environment(monkeypatch):
    monkeypatch.setenv("WEBHOOK_BEARER_TOKEN", "test-token")
    monkeypatch.setenv("ALLOWED_NAMESPACE", "production")
    monkeypatch.setenv("EKS_CLUSTER_NAME", "test-cluster")
    monkeypatch.setenv(
        "EKS_CLUSTER_ENDPOINT", "https://test-cluster.eks.amazonaws.com"
    )
    monkeypatch.setenv(
        "EKS_CLUSTER_CA_CERTIFICATE_BASE64",
        base64.b64encode(b"fake-ca-cert").decode("ascii"),
    )
    monkeypatch.setenv("AWS_REGION", "us-east-1")


def make_event(payload, authorization="Bearer test-token"):
    return {
        "headers": {"Authorization": authorization},
        "body": json.dumps(payload),
    }


def firing_payload(namespace="production", deployment="auth-service"):
    return {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "HighErrorRate",
                    "namespace": namespace,
                    "deployment": deployment,
                },
            }
        ],
    }


def response_body(response):
    return json.loads(response["body"])


def _mock_urlopen(status=200, body=b'{"kind":"Deployment"}'):
    """Create a mock that works as a context manager for urllib.request.urlopen."""
    mock_response = MagicMock()
    mock_response.__enter__.return_value = mock_response
    mock_response.__exit__.return_value = False
    mock_response.status = status
    mock_response.read.return_value = body
    return mock_response


def _mock_aws_credentials():
    from botocore.credentials import ReadOnlyCredentials

    mock_creds = MagicMock()
    mock_creds.get_frozen_credentials.return_value = ReadOnlyCredentials(
        "AKIATESTACCESSKEYID",
        "secretkeyfortesting12345678",
        None,
    )
    return mock_creds


# ---------------------------------------------------------------------------
# Task 1: Core utilities
# ---------------------------------------------------------------------------


def test_parse_payload_decodes_plain_json():
    event = {"body": json.dumps({"status": "firing", "alerts": []})}
    result = _parse_payload(event)
    assert result == {"status": "firing", "alerts": []}


def test_parse_payload_decodes_base64_encoded_body():
    payload = {"status": "firing", "alerts": []}
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    event = {"body": encoded, "isBase64Encoded": True}
    result = _parse_payload(event)
    assert result == payload


def test_parse_payload_rejects_malformed_json():
    event = {"body": "{not-json"}
    with pytest.raises(json.JSONDecodeError):
        _parse_payload(event)


def test_parse_payload_rejects_non_dict_body():
    event = {"body": json.dumps(["not", "a", "dict"])}
    with pytest.raises(ValueError, match="JSON object"):
        _parse_payload(event)


def test_response_formats_correctly():
    result = _response(200, {"result": "ok"})
    assert result["statusCode"] == 200
    assert result["headers"]["content-type"] == "application/json"
    assert json.loads(result["body"]) == {"result": "ok"}


# ---------------------------------------------------------------------------
# Task 2: Authentication and alert validation
# ---------------------------------------------------------------------------


def test_is_valid_deployment_name_accepts_valid_names():
    assert _is_valid_deployment_name("auth-service")
    assert _is_valid_deployment_name("flag-service")
    assert _is_valid_deployment_name("my-app-123")
    assert _is_valid_deployment_name("a.b.c")


def test_is_valid_deployment_name_rejects_invalid_names():
    assert not _is_valid_deployment_name("../etc")
    assert not _is_valid_deployment_name("")
    assert not _is_valid_deployment_name("UPPERCASE")
    assert not _is_valid_deployment_name("trailing-")
    assert not _is_valid_deployment_name(None)
    assert not _is_valid_deployment_name(123)


def test_handler_rejects_invalid_webhook_token(configured_environment):
    with patch("app.restart_deployment") as restart:
        response = handler(make_event(firing_payload(), "Bearer wrong"), None)
    assert response["statusCode"] == 401
    restart.assert_not_called()


def test_handler_rejects_non_post_requests(configured_environment):
    event = make_event(firing_payload())
    event["requestContext"] = {"http": {"method": "GET"}}
    response = handler(event, None)
    assert response["statusCode"] == 405


def test_handler_ignores_resolved_alert(configured_environment):
    with patch("app.restart_deployment") as restart:
        response = handler(make_event({"status": "resolved", "alerts": []}), None)
    assert response["statusCode"] == 200
    assert response_body(response)["result"] == "no firing alert"
    restart.assert_not_called()


def test_handler_extracts_firing_target(configured_environment):
    with patch("app.restart_deployment") as restart:
        response = handler(make_event(firing_payload()), None)
    assert response["statusCode"] == 200
    assert response_body(response) == {
        "result": "deployment restart requested",
        "deployment": "auth-service",
    }
    restart.assert_called_once_with("production", "auth-service")


def test_handler_rejects_alert_from_another_namespace(configured_environment):
    with patch("app.restart_deployment") as restart:
        response = handler(
            make_event(firing_payload(namespace="kube-system")), None
        )
    assert response["statusCode"] == 400
    restart.assert_not_called()


def test_handler_rejects_missing_deployment_label(configured_environment):
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "HighErrorRate",
                    "namespace": "production",
                },
            }
        ],
    }
    with patch("app.restart_deployment") as restart:
        response = handler(make_event(payload), None)
    assert response["statusCode"] == 400
    restart.assert_not_called()


def test_handler_rejects_invalid_deployment_label(configured_environment):
    with patch("app.restart_deployment") as restart:
        response = handler(
            make_event(firing_payload(deployment="../etc")), None
        )
    assert response["statusCode"] == 400
    restart.assert_not_called()


def test_handler_fails_closed_when_token_not_configured(
    configured_environment, monkeypatch
):
    monkeypatch.delenv("WEBHOOK_BEARER_TOKEN")
    response = handler(make_event(firing_payload()), None)
    assert response["statusCode"] == 500


def test_handler_accepts_base64_encoded_body(configured_environment):
    payload = base64.b64encode(
        json.dumps(firing_payload()).encode("utf-8")
    ).decode("ascii")
    event = {
        "headers": {"authorization": "Bearer test-token"},
        "body": payload,
        "isBase64Encoded": True,
    }
    with patch("app.restart_deployment") as restart:
        response = handler(event, None)
    assert response["statusCode"] == 200
    restart.assert_called_once()


# ---------------------------------------------------------------------------
# Task 3: EKS authentication
# ---------------------------------------------------------------------------


def test_eks_bearer_token_has_correct_prefix():
    mock_session = MagicMock()
    mock_session.get_credentials.return_value = _mock_aws_credentials()
    with patch("boto3.Session", return_value=mock_session):
        token = _eks_bearer_token("test-cluster", "us-east-1")
    assert token.startswith("k8s-aws-v1.")


def test_eks_bearer_token_contains_signed_sts_url():
    mock_session = MagicMock()
    mock_session.get_credentials.return_value = _mock_aws_credentials()
    with patch("boto3.Session", return_value=mock_session):
        token = _eks_bearer_token("test-cluster", "us-east-1")
    encoded = token.removeprefix("k8s-aws-v1.")
    padding = "=" * (-len(encoded) % 4)
    decoded_url = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
    assert "sts.us-east-1.amazonaws.com" in decoded_url
    assert "GetCallerIdentity" in decoded_url


def test_eks_bearer_token_raises_without_credentials():
    mock_session = MagicMock()
    mock_session.get_credentials.return_value = None
    with patch("boto3.Session", return_value=mock_session):
        with pytest.raises(RuntimeError, match="AWS credentials are unavailable"):
            _eks_bearer_token("test-cluster", "us-east-1")


# ---------------------------------------------------------------------------
# Task 4: Kubernetes API requests and rolling restart
# ---------------------------------------------------------------------------


def test_restart_deployment_sends_correct_patch(configured_environment):
    mock_response = _mock_urlopen()
    with (
        patch("app._eks_bearer_token", return_value="fake-token"),
        patch("ssl.create_default_context", return_value=MagicMock()),
        patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen,
    ):
        restart_deployment("production", "auth-service")
    mock_urlopen.assert_called_once()
    request = mock_urlopen.call_args.args[0]
    assert request.method == "PATCH"
    assert (
        request.full_url
        == "https://test-cluster.eks.amazonaws.com"
        "/apis/apps/v1/namespaces/production/deployments/auth-service"
    )


def test_restart_deployment_sets_restarted_at_annotation(configured_environment):
    mock_response = _mock_urlopen()
    with (
        patch("app._eks_bearer_token", return_value="fake-token"),
        patch("ssl.create_default_context", return_value=MagicMock()),
        patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen,
    ):
        restart_deployment("production", "auth-service")
    request = mock_urlopen.call_args.args[0]
    body = json.loads(request.data.decode("utf-8"))
    assert (
        "kubectl.kubernetes.io/restartedAt"
        in body["spec"]["template"]["metadata"]["annotations"]
    )


def test_restart_deployment_uses_strategic_merge_patch_content_type(
    configured_environment,
):
    mock_response = _mock_urlopen()
    with (
        patch("app._eks_bearer_token", return_value="fake-token"),
        patch("ssl.create_default_context", return_value=MagicMock()),
        patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen,
    ):
        restart_deployment("production", "auth-service")
    request = mock_urlopen.call_args.args[0]
    assert request.headers["Content-type"] == "application/strategic-merge-patch+json"


def test_restart_deployment_raises_on_404(configured_environment):
    http_error = urllib.error.HTTPError(
        url="https://test-cluster.eks.amazonaws.com/test",
        code=404,
        msg="Not Found",
        hdrs=None,
        fp=None,
    )
    with (
        patch("app._eks_bearer_token", return_value="fake-token"),
        patch("ssl.create_default_context", return_value=MagicMock()),
        patch("urllib.request.urlopen", side_effect=http_error),
    ):
        with pytest.raises(KubernetesApiError) as exc_info:
            restart_deployment("production", "auth-service")
    assert exc_info.value.status == 404


def test_restart_deployment_raises_on_403(configured_environment):
    http_error = urllib.error.HTTPError(
        url="https://test-cluster.eks.amazonaws.com/test",
        code=403,
        msg="Forbidden",
        hdrs=None,
        fp=None,
    )
    with (
        patch("app._eks_bearer_token", return_value="fake-token"),
        patch("ssl.create_default_context", return_value=MagicMock()),
        patch("urllib.request.urlopen", side_effect=http_error),
    ):
        with pytest.raises(KubernetesApiError) as exc_info:
            restart_deployment("production", "auth-service")
    assert exc_info.value.status == 403


def test_kubernetes_request_decodes_ca_certificate(configured_environment):
    mock_response = _mock_urlopen()
    with (
        patch("ssl.create_default_context", return_value=MagicMock()),
        patch("urllib.request.urlopen", return_value=mock_response),
    ):
        _kubernetes_request(
            "GET",
            "/api/v1/namespaces/production/pods",
            "fake-token",
            b"fake-ca-bytes",
        )
    ca_path = os.path.join(tempfile.gettempdir(), "self-healing-eks-ca.pem")
    with open(ca_path, "rb") as f:
        assert f.read() == b"fake-ca-bytes"


def test_restart_deployment_raises_on_empty_ca(configured_environment, monkeypatch):
    monkeypatch.setenv(
        "EKS_CLUSTER_CA_CERTIFICATE_BASE64",
        base64.b64encode(b"").decode("ascii"),
    )
    with pytest.raises(ValueError, match="CA certificate is empty"):
        restart_deployment("production", "auth-service")


# ---------------------------------------------------------------------------
# Task 5: Full handler wiring and integration
# ---------------------------------------------------------------------------


def test_handler_restarts_deployment_on_valid_alert(configured_environment):
    with patch("app.restart_deployment") as restart:
        response = handler(make_event(firing_payload()), None)
    assert response["statusCode"] == 200
    assert response_body(response) == {
        "result": "deployment restart requested",
        "deployment": "auth-service",
    }
    restart.assert_called_once_with("production", "auth-service")


def test_handler_treats_already_gone_deployment_as_noop(configured_environment):
    with patch(
        "app.restart_deployment",
        side_effect=KubernetesApiError(404),
    ):
        response = handler(make_event(firing_payload()), None)
    assert response["statusCode"] == 200
    assert response_body(response)["result"] == "deployment no longer exists"


def test_handler_reports_authorization_failure(configured_environment):
    with patch(
        "app.restart_deployment",
        side_effect=KubernetesApiError(403),
    ):
        response = handler(make_event(firing_payload()), None)
    assert response["statusCode"] == 502
    assert "authorization" in response_body(response)["error"].lower()


def test_handler_reports_kubernetes_failure(configured_environment):
    with patch(
        "app.restart_deployment",
        side_effect=KubernetesApiError(500),
    ):
        response = handler(make_event(firing_payload()), None)
    assert response["statusCode"] == 502


def test_handler_reports_unexpected_failure(configured_environment):
    with patch(
        "app.restart_deployment",
        side_effect=RuntimeError("unexpected"),
    ):
        response = handler(make_event(firing_payload()), None)
    assert response["statusCode"] == 500


def test_handler_malformed_json_returns_400(configured_environment):
    event = {
        "headers": {"Authorization": "Bearer test-token"},
        "body": "{not-json",
    }
    response = handler(event, None)
    assert response["statusCode"] == 400
