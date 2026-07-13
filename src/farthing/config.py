"""Runtime configuration and the structural credential split.

The single most important thing here: Stripe uses TWO restricted keys.

* ``stripe_prepare_key`` — read + refund-*prepare* scope — is the only Stripe
  credential the agent worker process is given. It can look up invoices and build
  a refund preview. It cannot execute a refund.
* ``stripe_execute_key`` — refund-*execute* scope — lives ONLY in the approval
  service process. It is the only credential that can actually move money, and it
  is only ever exercised after a human approves.

If both keys are present in the same process environment, the least-privilege
claim is false. ``assert_credential_split`` makes that a startup error, not a
subtle lie. Load the two roles from different secret sources / different
containers in production.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    """Config for the agent worker. Holds the PREPARE key ONLY."""

    model_config = SettingsConfigDict(env_prefix="FARTHING_", env_file=".env", extra="ignore")

    stripe_prepare_key: SecretStr = Field(..., description="Stripe restricted key: read + refund-prepare")
    anthropic_api_key: SecretStr
    extraction_model: str = "anthropic:claude-sonnet-5"
    escalation_model: str = "anthropic:claude-opus-4-8"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    task_queue: str = "farthing-agent"
    # Read-only access to the ledger DB for the nightly reconciliation activity.
    reconcile_database_url: SecretStr | None = Field(default=None)

    # Guardrail: the execute key must NOT be visible to the worker.
    stripe_execute_key: SecretStr | None = Field(default=None)


class ApprovalSettings(BaseSettings):
    """Config for the approval service. Holds the EXECUTE key and the signing
    secret — and must NOT hold anything the agent worker needs."""

    model_config = SettingsConfigDict(env_prefix="FARTHING_APPROVAL_", env_file=".env", extra="ignore")

    stripe_execute_key: SecretStr = Field(..., description="Stripe restricted key: refund-execute")
    approval_signing_secret: SecretStr = Field(..., description="HMAC secret for approval tokens")
    database_url: SecretStr = Field(..., description="Postgres URL for the durable idempotency store")
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    execute_task_queue: str = "farthing-execute"
    # Header the upstream auth proxy sets with the authenticated reviewer identity.
    reviewer_email_header: str = "x-reviewer-email"
    # Shared secret the auth proxy sends (X-Proxy-Auth) so a client that reaches
    # the app directly and forges the identity header still cannot authenticate.
    # REQUIRED — the identity header alone is spoofable without it.
    proxy_shared_secret: SecretStr = Field(..., description="Shared secret proving the request came via the auth proxy")


def assert_credential_split(worker: WorkerSettings) -> None:
    """Fail fast if the worker environment can see the execute key."""
    if worker.stripe_execute_key is not None:
        raise RuntimeError(
            "FARTHING_STRIPE_EXECUTE_KEY is visible to the agent worker. "
            "The execute credential must live ONLY in the approval service. "
            "This breaks least-privilege — refusing to start."
        )


@lru_cache
def worker_settings() -> WorkerSettings:
    s = WorkerSettings()  # type: ignore[call-arg]
    assert_credential_split(s)
    return s


@lru_cache
def approval_settings() -> ApprovalSettings:
    return ApprovalSettings()  # type: ignore[call-arg]
