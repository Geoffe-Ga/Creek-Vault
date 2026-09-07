"""Lease-driven provisioning worker that performs no work in API requests (#1768)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from creek_mcp.provisioning.driver import (
    HandoffError,
    OneTimeCredentialHandoff,
    ProviderDriver,
    ProviderError,
)
from creek_mcp.provisioning.models import FailureReason, JobOperation
from creek_mcp.provisioning.store import LostJobLeaseError

if TYPE_CHECKING:
    from creek_mcp.provisioning.store import ProvisioningStore

_LOGGER = logging.getLogger(__name__)


class ProvisioningWorker:
    """Claim and settle at most one durable provider operation per invocation."""

    def __init__(
        self,
        store: ProvisioningStore,
        driver: ProviderDriver,
        handoff: OneTimeCredentialHandoff,
        *,
        lease_for: timedelta = timedelta(minutes=1),
    ) -> None:
        """Bind the durable queue to injected provider and handoff adapters."""
        self._store = store
        self._driver = driver
        self._handoff = handoff
        self._lease_for = lease_for

    def run_once(self, *, now: datetime | None = None) -> bool:
        """Process one claim and return whether work was available."""
        claimed = self._store.claim_next(now=now, lease_for=self._lease_for)
        if claimed is None:
            return False
        try:
            if claimed.job.operation is JobOperation.DELETE:
                self._driver.delete(
                    claimed.job.job_id,
                    claimed.provider_allocation_id,
                )
                self._store.complete_delete(
                    claimed.job.job_id,
                    claimed.lease_token,
                    now=now,
                )
            else:
                allocation = self._driver.provision(
                    claimed.job.job_id,
                    claimed.job.consumer_identity,
                )
                if not self._store.owns_lease(
                    claimed.job.job_id,
                    claimed.lease_token,
                    now=now,
                ):
                    _LOGGER.info(
                        "provisioning result discarded after lease loss job_id=%s",
                        claimed.job.job_id,
                    )
                    return True
                self._handoff.deliver(
                    claimed.job.job_id,
                    claimed.job.consumer_identity,
                    allocation.vault_url,
                    allocation.consumer_credential,
                )
                self._store.complete_create(
                    claimed.job.job_id,
                    claimed.lease_token,
                    allocation.allocation_id,
                    now=now,
                )
        except ProviderError as failure:
            self._record_failure(
                claimed.job.job_id,
                claimed.lease_token,
                failure.reason,
                retryable=failure.retryable,
                now=now,
            )
        except HandoffError:
            self._record_failure(
                claimed.job.job_id,
                claimed.lease_token,
                FailureReason.HANDOFF_FAILED,
                retryable=False,
                now=now,
            )
        except LostJobLeaseError:
            _LOGGER.info(
                "provisioning result discarded after lease loss job_id=%s",
                claimed.job.job_id,
            )
        except Exception:
            self._record_failure(
                claimed.job.job_id,
                claimed.lease_token,
                FailureReason.INTERNAL_ERROR,
                retryable=True,
                now=now,
            )
        return True

    def _record_failure(
        self,
        job_id: str,
        lease_token: str,
        reason: FailureReason,
        *,
        retryable: bool,
        now: datetime | None,
    ) -> None:
        """Persist and log only the stable, content-free failure classification."""
        self._store.record_failure(
            job_id,
            lease_token,
            reason,
            retryable=retryable,
            now=now,
        )
        _LOGGER.info(
            "provisioning job failed job_id=%s reason=%s retryable=%s",
            job_id,
            reason.value,
            retryable,
        )
