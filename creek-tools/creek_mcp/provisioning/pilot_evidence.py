"""Offline sanitizer for the managed-vault Fly pilot evidence (#1806).

The input is a private operator record.  It may contain provider identifiers,
addresses, subjects, and local artifact paths.  The output is a closed,
content-free prerequisite block: cardinalities are derived, artifacts are
hashed locally, and private values are never copied.  This module performs no
network request and no provider mutation.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

_CLOSED: Final[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)
Sha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]


def _valid_utc_seconds(value: str) -> str:
    """Reject lexically valid but impossible UTC calendar timestamps."""
    datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    return value


def _valid_date(value: str) -> str:
    """Reject lexically valid but impossible calendar dates."""
    date.fromisoformat(value)
    return value


UtcSeconds = Annotated[
    str,
    StringConstraints(
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
    ),
    AfterValidator(_valid_utc_seconds),
]
SignedUsd = Annotated[
    str,
    StringConstraints(pattern=r"^-?(0|[1-9]\d*)\.\d{2}$"),
]
UnsignedUsd = Annotated[
    str,
    StringConstraints(pattern=r"^(0|[1-9]\d*)\.\d{2}$"),
]
CalendarDate = Annotated[
    str,
    StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$"),
    AfterValidator(_valid_date),
]

ARTIFACT_KINDS: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "control_image",
        "vault_image",
        "deployments_health",
        "create_after_provider_create_before_handoff",
        "provider_outage",
        "callback_outage",
        "readiness_timeout",
        "routing_failure",
        "cardinality_teardown",
        "fleet_report",
        "alert_delivery",
        "reconcile",
        "sqlite_runtime_state_restore",
        "stopped_volume_restore",
        "emergency_stop",
        "invoice_cost",
        "exact_main",
        "independent_review",
    }
)
ArtifactKind = Literal[
    "authorization",
    "control_image",
    "vault_image",
    "deployments_health",
    "create_after_provider_create_before_handoff",
    "provider_outage",
    "callback_outage",
    "readiness_timeout",
    "routing_failure",
    "cardinality_teardown",
    "fleet_report",
    "alert_delivery",
    "reconcile",
    "sqlite_runtime_state_restore",
    "stopped_volume_restore",
    "emergency_stop",
    "invoice_cost",
    "exact_main",
    "independent_review",
]


class SourceFacts(BaseModel):
    """Immutable source revisions and image digests."""

    model_config = _CLOSED
    creek_sha: Sha
    control_image_digest: Digest
    vault_image_digest: Digest
    provisioning_contract: Literal["2.1.0"]
    vault_contract: Literal["0.16.0"]


class AuthorizationFacts(BaseModel):
    """Explicit pilot limits, with no organization or owner identity."""

    model_config = _CLOSED
    dedicated_organization: Literal[True]
    max_live_allocations: Annotated[int, Field(ge=1, le=5)]
    currency: Literal["USD"]
    monthly_alert_usd_cents: Literal[2500]
    region_recorded: Literal[True]
    cleanup_owner_recorded: Literal[True]


class CredentialFacts(BaseModel):
    """Credential scope, placement, lifetime, and final revocation facts."""

    model_config = _CLOSED
    deploy_token_scope: Literal["organization_deploy"]
    evidence_token_scope: Literal["organization_read_only"]
    deploy_token_lifetime_seconds: Annotated[int, Field(ge=1, le=604800)]
    evidence_token_lifetime_seconds: Annotated[int, Field(ge=1, le=604800)]
    deploy_token_age_seconds: Annotated[int, Field(ge=0, le=604800)]
    evidence_token_age_seconds: Annotated[int, Field(ge=0, le=604800)]
    personal_token_values: Literal[0]
    secret_values_in_environment: Literal[0]
    mounted_secret_file_modes_0400: Literal[True]
    deploy_token_revoked: Literal[True]
    evidence_token_revoked: Literal[True]

    @model_validator(mode="after")
    def require_age_within_lifetime(self) -> CredentialFacts:
        """Reject observations made after either declared credential lifetime."""
        if self.deploy_token_age_seconds > self.deploy_token_lifetime_seconds:
            raise ValueError("deploy token age exceeds its declared lifetime")
        if self.evidence_token_age_seconds > self.evidence_token_lifetime_seconds:
            raise ValueError("evidence token age exceeds its declared lifetime")
        return self


class DeploymentFacts(BaseModel):
    """Deployed immutable revisions and health boundaries."""

    model_config = _CLOSED
    control_revision: Sha
    worker_revision: Sha
    routing_revision: Sha
    fleet_schedule_revision: Sha
    control_image_digest_matches_deployment: Literal[True]
    vault_image_digest_matches_deployment: Literal[True]
    shared_atomic_state_storage: Literal[True]
    public_tls_healthy: Literal[True]
    authenticated_health_checks_healthy: Literal[True]
    worker_ready_signal_healthy: Literal[True]


class DrillFacts(BaseModel):
    """Proof shared by every bounded, reversible fault drill."""

    model_config = _CLOSED
    outcome: Literal["passed"]
    reversible: Literal[True]
    destructive_provider_mutations: Literal[0]
    unexpected_billable_resources: Literal[0]


class CreateBeforeHandoffDrill(DrillFacts):
    """Worker termination after provider create and before handoff."""

    retryable_job_persisted: Literal[True]
    restarted_worker_converged: Literal[True]


class ProviderOutageDrill(DrillFacts):
    """Scoped worker-to-provider outage proof."""

    provider_unavailable_persisted: Literal[True]
    retry_converged: Literal[True]


class CallbackOutageDrill(DrillFacts):
    """Scoped worker-to-callback outage proof."""

    handoff_failure_persisted: Literal[True]
    retry_converged: Literal[True]


class ReadinessTimeoutDrill(DrillFacts):
    """Bounded provider readiness timeout proof."""

    route_refused: Literal[True]
    same_machine_after_restore: Literal[True]


class RoutingFailureDrill(DrillFacts):
    """Public routing outage and restoration proof."""

    unavailable_response_observed: Literal[True]
    same_endpoint_after_restore: Literal[True]


class FaultDrills(BaseModel):
    """The complete reviewed fault-drill set."""

    model_config = _CLOSED
    create_after_provider_create_before_handoff: CreateBeforeHandoffDrill
    provider_outage: ProviderOutageDrill
    callback_outage: CallbackOutageDrill
    readiness_timeout: ReadinessTimeoutDrill
    routing_failure: RoutingFailureDrill


class PrivateIdentifiers(BaseModel):
    """Private allocation-scoped identifiers used only to derive counts."""

    model_config = _CLOSED
    apps: list[str]
    machines: list[str]
    volumes: list[str]
    credentials: list[str]
    jobs: list[str]

    @model_validator(mode="after")
    def require_unique_values(self) -> PrivateIdentifiers:
        """Reject duplicate observations that would conceal cardinality drift."""
        if any(
            len(values) != len(set(values)) for values in self.model_dump().values()
        ):
            raise ValueError("private resource identifiers must be unique")
        return self


class PrivateCardinality(BaseModel):
    """Private provider/control observations for one pilot allocation."""

    model_config = _CLOSED
    after_create: PrivateIdentifiers
    after_teardown: PrivateIdentifiers
    teardown_receipt_confirmed: Literal[True]
    teardown_idempotent: Literal[True]


class ResourceCounts(BaseModel):
    """Content-free allocation-scoped resource cardinalities."""

    model_config = _CLOSED
    app_count: Annotated[int, Field(ge=0)]
    machine_count: Annotated[int, Field(ge=0)]
    volume_count: Annotated[int, Field(ge=0)]
    credential_count: Annotated[int, Field(ge=0)]
    job_count: Annotated[int, Field(ge=0)]


class CardinalityFacts(BaseModel):
    """Derived allocation-scoped create and teardown cardinalities."""

    model_config = _CLOSED
    after_create: ResourceCounts
    after_teardown: ResourceCounts
    teardown_receipt_confirmed: Literal[True]
    teardown_idempotent: Literal[True]


class FleetFacts(BaseModel):
    """Provider-authoritative fleet, alert, and reconcile outcomes."""

    model_config = _CLOSED
    provider_authoritative_inventory: Literal[True]
    inventory_mode: Literal["derived", "derived+injected"]
    alert_test_report_exit_code: Literal[3]
    alert_kind: Literal["monthly_budget_departure"]
    alert_delivered: Literal[True]
    alert_acknowledged: Literal[True]
    final_report_exit_code: Literal[0]
    final_alert_count: Literal[0]
    final_divergence_count: Literal[0]
    reconcile_window_approved: Literal[True]
    reconcile_interruption_window_seconds: Annotated[int, Field(ge=1, le=86400)]
    retryable_deletions_requeued: Annotated[int, Field(ge=1)]
    nonretryable_deletions_requeued: Literal[0]


class SqliteRestoreFacts(BaseModel):
    """Atomic SQLite plus encrypted runtime-state restore proof."""

    model_config = _CLOSED
    outcome: Literal["passed"]
    network_mutations_disabled: Literal[True]
    schema_valid: Literal[True]
    job_membership_equal: Literal[True]
    allocation_membership_equal: Literal[True]
    credential_membership_equal: Literal[True]
    receipt_membership_equal: Literal[True]
    idempotent_restart: Literal[True]


class VolumeRestoreFacts(BaseModel):
    """Stopped-volume snapshot restore proof."""

    model_config = _CLOSED
    outcome: Literal["passed"]
    source_machine_stopped: Literal[True]
    snapshot_created: Literal[True]
    same_region: Literal[True]
    reviewed_image_digest_matched: Literal[True]
    sentinel_equal: Literal[True]
    original_volume_preserved: Literal[True]
    temporary_machine_removed: Literal[True]
    temporary_volume_removed: Literal[True]


class RestoreFacts(BaseModel):
    """Both required restore proofs."""

    model_config = _CLOSED
    sqlite_runtime_state: SqliteRestoreFacts
    stopped_volume: VolumeRestoreFacts


class EmergencyStopFacts(BaseModel):
    """Non-destructive stop and subsequent recovery proof."""

    model_config = _CLOSED
    outcome: Literal["passed"]
    non_destructive: Literal[True]
    stopped_count: Annotated[int, Field(ge=1, le=5)]
    failed_count: Literal[0]
    routing_recovered: Literal[True]
    deletion_converged: Literal[True]


class CostFacts(BaseModel):
    """Dated provider invoice reconciliation without float ambiguity."""

    model_config = _CLOSED
    currency: Literal["USD"]
    estimated_usd: UnsignedUsd
    provider_total_usd: UnsignedUsd
    signed_delta_usd: SignedUsd
    rate_date: CalendarDate
    rate_source: Literal["provider_invoice", "provider_usage_sample"]
    unpriced_inputs: list[
        Literal["egress", "snapshot", "stopped_rootfs", "volume", "compute"]
    ]
    unknown_inputs_treated_as_zero: Literal[False]
    reconciled: Literal[True]

    @field_validator("unpriced_inputs")
    @classmethod
    def require_sorted_unique_inputs(cls, value: list[str]) -> list[str]:
        """Keep the public component list deterministic and duplicate-free."""
        if value:
            raise ValueError("clean final report requires no unpriced inputs")
        return value

    @model_validator(mode="after")
    def require_truthful_delta(self) -> CostFacts:
        """Bind the signed delta to provider total minus Creek estimate."""
        expected = Decimal(self.provider_total_usd) - Decimal(self.estimated_usd)
        if expected != Decimal(self.signed_delta_usd):
            raise ValueError("signed cost delta does not reconcile")
        return self


class ExactMainFacts(BaseModel):
    """Frozen-main CI and deployment verification facts."""

    model_config = _CLOSED
    ci_green: Literal[True]
    deployment_verified: Literal[True]
    unexplained_product_skips: Literal[0]


class PrivateReviewFacts(BaseModel):
    """Review reference before the private artifact hash is attached."""

    model_config = _CLOSED
    reviewed_creek_sha: Sha
    verdict: Literal["LGTM"]
    reference_kind: Literal["github_issuecomment"]
    reference_number: Annotated[int, Field(gt=0)]
    artifact_kind: Literal["independent_review"]


class ReviewFacts(PrivateReviewFacts):
    """Hash-bound independent review reference."""

    artifact_sha256: Digest


class PrivateArtifact(BaseModel):
    """Local private artifact reference; the path is never exported."""

    model_config = _CLOSED
    kind: ArtifactKind
    observed_at: UtcSeconds
    path: str


class ArtifactFact(BaseModel):
    """Public content-free artifact hash and observation time."""

    model_config = _CLOSED
    kind: ArtifactKind
    observed_at: UtcSeconds
    sha256: Digest


class PrivateContext(BaseModel):
    """Private references accepted only to prove the sanitizer drops them."""

    model_config = _CLOSED
    organization_id: str
    alert_subject: str
    vault_url: str


class PilotEvidenceInput(BaseModel):
    """Closed private input consumed by the non-networking reducer."""

    model_config = _CLOSED
    schema_version: Literal["1.0.0"]
    executed_at: UtcSeconds
    source: SourceFacts
    authorization: AuthorizationFacts
    credentials: CredentialFacts
    deployments: DeploymentFacts
    fault_drills: FaultDrills
    private_cardinality: PrivateCardinality
    fleet: FleetFacts
    restores: RestoreFacts
    emergency_stop: EmergencyStopFacts
    cost: CostFacts
    exact_main: ExactMainFacts
    independent_review: PrivateReviewFacts
    private_artifacts: list[PrivateArtifact]
    private_context: PrivateContext


class PilotPrerequisite(BaseModel):
    """Versioned sanitized prerequisite block embedded by Adepthood #2871."""

    model_config = _CLOSED
    schema_version: Literal["1.0.0"]
    status: Literal["passed"]
    executed_at: UtcSeconds
    source: SourceFacts
    authorization: AuthorizationFacts
    credentials: CredentialFacts
    deployments: DeploymentFacts
    fault_drills: FaultDrills
    cardinality: CardinalityFacts
    fleet: FleetFacts
    restores: RestoreFacts
    emergency_stop: EmergencyStopFacts
    cost: CostFacts
    exact_main: ExactMainFacts
    independent_review: ReviewFacts
    artifacts: list[ArtifactFact]


class PilotPrerequisiteDocument(BaseModel):
    """Named root document embedded verbatim by the Adepthood pilot proof."""

    model_config = _CLOSED
    managed_vault_pilot_prerequisite: PilotPrerequisite


def _counts(values: PrivateIdentifiers) -> ResourceCounts:
    """Reduce private identifier lists to content-free counts."""
    return ResourceCounts(
        app_count=len(values.apps),
        machine_count=len(values.machines),
        volume_count=len(values.volumes),
        credential_count=len(values.credentials),
        job_count=len(values.jobs),
    )


def _hash_artifact(artifact: PrivateArtifact) -> ArtifactFact:
    """Hash one readable regular file without exporting its private path."""
    path = Path(artifact.path)
    try:
        if not path.is_file():
            raise OSError
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError("private artifact is unreadable") from exc
    digest = hashlib.sha256(payload).hexdigest()
    return ArtifactFact(
        kind=artifact.kind,
        observed_at=artifact.observed_at,
        sha256=f"sha256:{digest}",
    )


def reduce_pilot_evidence(source: PilotEvidenceInput) -> PilotPrerequisite:
    """Return the validated content-free pilot prerequisite without networking."""
    artifacts = sorted(
        (_hash_artifact(item) for item in source.private_artifacts),
        key=lambda item: item.kind,
    )
    if {item.kind for item in artifacts} != ARTIFACT_KINDS or len(artifacts) != len(
        ARTIFACT_KINDS
    ):
        raise ValueError("private artifact kind set must be exact and unique")
    after_create = _counts(source.private_cardinality.after_create)
    after_teardown = _counts(source.private_cardinality.after_teardown)
    if set(after_create.model_dump().values()) != {1}:
        raise ValueError("after-create cardinality must be exactly one of each kind")
    if set(after_teardown.model_dump().values()) != {0}:
        raise ValueError("after-teardown cardinality must be zero")
    if source.independent_review.reviewed_creek_sha != source.source.creek_sha:
        raise ValueError("independent review must bind the reviewed Creek SHA")
    review_hash = next(
        item.sha256 for item in artifacts if item.kind == "independent_review"
    )
    return PilotPrerequisite(
        schema_version=source.schema_version,
        status="passed",
        executed_at=source.executed_at,
        source=source.source,
        authorization=source.authorization,
        credentials=source.credentials,
        deployments=source.deployments,
        fault_drills=source.fault_drills,
        cardinality=CardinalityFacts(
            after_create=after_create,
            after_teardown=after_teardown,
            teardown_receipt_confirmed=source.private_cardinality.teardown_receipt_confirmed,
            teardown_idempotent=source.private_cardinality.teardown_idempotent,
        ),
        fleet=source.fleet,
        restores=source.restores,
        emergency_stop=source.emergency_stop,
        cost=source.cost,
        exact_main=source.exact_main,
        independent_review=ReviewFacts(
            **source.independent_review.model_dump(),
            artifact_sha256=review_hash,
        ),
        artifacts=artifacts,
    )
