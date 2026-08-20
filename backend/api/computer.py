"""Authenticated cloud-only endpoint for current computer snapshots."""

import hashlib
import hmac
import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError

from computer.models import ComputerSnapshot
from computer.state import RemoteDeviceStateStore
from core.deployment import DeploymentSettings


router = APIRouter()
_LOGGER = logging.getLogger("computer.report_api")
_MAX_SNAPSHOT_BYTES = 32 * 1024
_MAX_FUTURE_SKEW = timedelta(seconds=15)
_MAX_REPORT_AGE = timedelta(seconds=45)
_MIN_REPORT_TOKEN_BYTES = 32
_MAX_REPORT_TOKEN_BYTES = 256


def _deployment_settings(request: Request) -> DeploymentSettings:
    return request.app.state.deployment_settings


def _remote_store(request: Request) -> RemoteDeviceStateStore:
    store = getattr(request.app.state.runtime, "computer_remote_state_store", None)
    if not isinstance(store, RemoteDeviceStateStore):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="computer_state_store_unavailable",
        )
    return store


def _authorize(request: Request, expected_token: str | None) -> None:
    if not expected_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="computer_state_report_not_configured",
        )
    getlist = getattr(request.headers, "getlist", None)
    if callable(getlist):
        values = list(getlist("authorization"))
    else:
        authorization = request.headers.get("authorization")
        values = [] if authorization is None else [authorization]
    prefix = "Bearer "
    valid_format = len(values) == 1 and isinstance(values[0], str)
    authorization = values[0] if valid_format else ""
    valid_format = valid_format and authorization.startswith(prefix)
    candidate = authorization[len(prefix) :] if valid_format else ""
    valid_candidate = (
        _MIN_REPORT_TOKEN_BYTES <= len(candidate) <= _MAX_REPORT_TOKEN_BYTES
        and candidate.isascii()
        and all("!" <= character <= "~" for character in candidate)
    )
    candidate_bytes = candidate.encode("ascii") if valid_candidate else b""
    candidate_digest = hashlib.sha256(candidate_bytes).digest()
    expected_digest = hashlib.sha256(expected_token.encode("ascii")).digest()
    authenticated = hmac.compare_digest(candidate_digest, expected_digest)
    if not valid_format or not valid_candidate or not authenticated:
        _LOGGER.warning("computer state report rejected status=unauthorized")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_report_token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def _read_limited_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length, 10)
        except ValueError:
            declared_size = -1
        if declared_size < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid_content_length",
            )
        if declared_size > _MAX_SNAPSHOT_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="snapshot_too_large",
            )

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_SNAPSHOT_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="snapshot_too_large",
            )
        body.extend(chunk)
    return bytes(body)


def _validate_report_time(
    snapshot: ComputerSnapshot,
    store: RemoteDeviceStateStore,
) -> None:
    if (
        snapshot.collected_at.utcoffset() != timedelta(0)
        or snapshot.expires_at.utcoffset() != timedelta(0)
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid_snapshot",
        )
    now = store.now()
    if not (
        now - _MAX_REPORT_AGE < snapshot.collected_at
        <= now + _MAX_FUTURE_SKEW
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="snapshot_time_invalid",
        )


@router.post("/computer/state", status_code=status.HTTP_202_ACCEPTED)
async def receive_computer_state(
    request: Request,
    store: RemoteDeviceStateStore = Depends(_remote_store),
    settings: DeploymentSettings = Depends(_deployment_settings),
) -> dict[str, str]:
    """Accept one bounded, privacy-filtered snapshot from the configured Mac."""

    if settings.profile != "cloud":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    _authorize(request, settings.computer_state_report_token)
    raw = await _read_limited_body(request)
    try:
        snapshot = ComputerSnapshot.model_validate_json(raw)
    except (ValidationError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid_snapshot",
        ) from None
    if snapshot.device_id != settings.computer_default_device_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="device_not_allowed",
        )
    _validate_report_time(snapshot, store)
    if not store.put(snapshot):
        _LOGGER.info(
            "computer state report rejected device=%s status=not_newer",
            snapshot.device_id,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="snapshot_not_newer",
        )
    _LOGGER.info(
        "computer state report accepted device=%s status=accepted",
        snapshot.device_id,
    )
    return {"status": "accepted", "deviceId": snapshot.device_id}
