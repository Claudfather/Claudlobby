"""One selected-fleet notification operation and its recording policy."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from .activation_state import read_selection
from .context import resolve_paths
from .env_tiers import resolve as resolve_tiers
from .message_context import _transport
from .operation_context import resolve_operation_scope
from .plane.emit_api import emit_batch
from .plane.ids import mint_event_id
from .recording_alerts import (_TIER_KEYS, ChannelOutcome, RecordingAlertOutcome,
                               send_fleet_notification)
from .runtime_admission import RuntimeIdentity, mutation_admission


class FleetNotificationError(ValueError):
    pass


class FleetNotificationInputError(FleetNotificationError):
    pass


@dataclass(frozen=True)
class FleetNotificationResult:
    fleet: str
    release_id: str
    event_id: str
    request_id: str
    level: str
    event: str
    recording: str
    manager: ChannelOutcome
    telegram: ChannelOutcome

    def data(self) -> dict:
        result = asdict(self)
        result["notification"] = ("submitted" if self.manager.status == "submitted"
                                  or self.telegram.status == "carrier_accepted" else
                                  "suppressed" if "suppressed" in (self.manager.status,
                                                                     self.telegram.status) else "failed")
        return result


def notify_fleet(*, root: Path | None, fleet: str | None, level: str,
                 event: str, message: str, identity: RuntimeIdentity | None = None,
                 emit=emit_batch, send=send_fleet_notification) -> FleetNotificationResult:
    if level not in {"alert", "notice"}:
        raise FleetNotificationInputError("--level must be alert or notice")
    if not isinstance(event, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", event):
        raise FleetNotificationInputError("--event must be a literal lower-case identifier")
    if (not isinstance(message, str) or not message.strip() or not message.isprintable()
            or len(message.encode("utf-8")) > 2000):
        raise FleetNotificationInputError("--message must be printable text of at most 2000 UTF-8 bytes")
    selected_root = resolve_paths(root=root).root
    bound_release = os.environ.get("CLAUDLOBBY_RELEASE_ID")
    with mutation_admission(selected_root, identity=identity or RuntimeIdentity.current(),
                            expected_release=bound_release) as release:
        selected, origin = resolve_operation_scope(root=selected_root, fleet=fleet)
        if origin is not None and (origin.fleet.name != selected.fleet.name
                                   or origin.bot_id != selected.fleet.manager):
            raise FleetNotificationError("only the selected fleet manager may notify its fleet")
        if origin is not None and (not bound_release
                                   or not re.fullmatch(r"r-[0-9a-f]{64}", bound_release)):
            raise FleetNotificationError("generated manager lacks a bound selected release")
        selection = read_selection(selected.paths.root)
        if (selection is None or selection["release_id"] != release.release_id
                or selected.paths.package.native != release.native_path):
            raise FleetNotificationError("notification scope differs from selected release")
        destination = _transport(selected, selected.fleet.manager)
        try:
            tiers = resolve_tiers(selected.paths, bot_name=selected.fleet.manager,
                                  fleet_name=selected.fleet.name)
            trusted = {key: tiers[key].value for key in _TIER_KEYS if key in tiers}
            tiers_available = True
        except (OSError, ValueError, RuntimeError):
            trusted = {}
            tiers_available = False
        event_id = mint_event_id()
        request_id = str(uuid4())
        at = datetime.now(timezone.utc)
        raw = {"event_id": event_id, "event_type": "system", "emitter": "fleet-notify",
               "fleet": selected.fleet.name,
               "payload": {"event": "fleet_alert" if level == "alert" else "fleet_notice",
                           "subject_kind": "fleet", "subject": selected.fleet.name,
                           "data": {"event": event, "message": message,
                                    "request_id": request_id, "at": at.isoformat()}}}
        try:
            recorded = emit(selected.paths.root, [raw], require_commit=True)[0]
            recording = "committed" if recorded.status in {"committed", "duplicate"} else "unknown"
        except (OSError, sqlite3.Error):
            # O1: a recording outage cannot block an independently configured alert.
            recording = "unknown"
        text = f"FLEET {level.upper()} [{event}]: {message}"
        try:
            channels = send(selected, selected.paths.package, destination,
                            message=text, event=event, request_id=request_id,
                            at=at, trusted_tiers=trusted)
        except Exception:
            channels = RecordingAlertOutcome(ChannelOutcome("failed", None, "adapter_error"),
                                             ChannelOutcome("failed", None, "adapter_error"))
        if not tiers_available and channels.telegram.status == "unconfigured":
            channels = RecordingAlertOutcome(channels.manager,
                                             ChannelOutcome("failed", None, "tier_unavailable"))
        return FleetNotificationResult(selected.fleet.name, release.release_id, event_id,
                                       request_id,
                                       level, event, recording,
                                       channels.manager, channels.telegram)
