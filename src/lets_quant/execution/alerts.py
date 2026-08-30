from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


ALERT_POLICY_SCHEMA_VERSION = 1
ALERT_STATE_SCHEMA_VERSION = 1
DELIVERY_RECEIPT_SCHEMA_VERSION = 1
AUDIT_REPORT_SCHEMA_VERSION = 1
SEVERITIES = ("critical", "warning")
ALERT_STATUSES = {"open", "acknowledged", "silenced", "resolved"}
ACTION_KINDS = {"acknowledge", "silence", "unsilence"}
DELIVERY_LEVELS = {"standard", "escalated"}
LOCAL_CHANNEL = "local_jsonl"


class PaperAlertError(ValueError):
    """Raised when the offline alert lifecycle contract is violated."""


@dataclass(frozen=True)
class SeverityPolicy:
    repeat_interval_seconds: int
    escalate_after_seconds: int


@dataclass(frozen=True)
class AlertPolicy:
    channel: str
    severities: Dict[str, SeverityPolicy]
    policy_sha256: str


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _expect_keys(
    value: Mapping[str, Any],
    *,
    required: Sequence[str],
    allowed: Sequence[str],
    path: str,
) -> None:
    missing = sorted(set(required) - set(value))
    unknown = sorted(set(value) - set(allowed))
    if missing:
        raise PaperAlertError(f"{path} is missing fields: {', '.join(missing)}")
    if unknown:
        raise PaperAlertError(f"{path} has unknown fields: {', '.join(unknown)}")


def _object(value: Any, path: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise PaperAlertError(f"{path} must be an object")
    return dict(value)


def _array(value: Any, path: str) -> List[Any]:
    if not isinstance(value, list):
        raise PaperAlertError(f"{path} must be an array")
    return list(value)


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PaperAlertError(f"{path} must be a non-empty string")
    return value


def _optional_string(value: Any, path: str) -> Optional[str]:
    if value is None:
        return None
    return _string(value, path)


def _integer(value: Any, path: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PaperAlertError(f"{path} must be an integer >= {minimum}")
    return value


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise PaperAlertError(f"{path} must be a boolean")
    return value


def _timestamp(value: Any, path: str) -> datetime:
    text = _string(value, path)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaperAlertError(f"{path} contains an invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PaperAlertError(f"{path} timestamp must include a timezone")
    return parsed


def _aware_timestamp(value: datetime, path: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PaperAlertError(f"{path} must include a timezone")
    return value


def _read_json(path: Path, label: str) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PaperAlertError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PaperAlertError(
            f"invalid {label} JSON at line {exc.lineno}"
        ) from exc
    return _object(value, label)


def _write_json(payload: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_alert_policy(path: Path) -> AlertPolicy:
    raw = _read_json(path, "paper alert policy")
    fields = ("schema_version", "channel", "severities")
    _expect_keys(raw, required=fields, allowed=fields, path="paper alert policy")
    if raw["schema_version"] != ALERT_POLICY_SCHEMA_VERSION:
        raise PaperAlertError("unsupported paper alert policy schema")
    channel = _string(raw["channel"], "paper alert policy.channel")
    if channel != LOCAL_CHANNEL:
        raise PaperAlertError(
            "paper alert policy.channel must be local_jsonl; external delivery "
            "is not implemented"
        )
    severity_values = _object(
        raw["severities"], "paper alert policy.severities"
    )
    if set(severity_values) != set(SEVERITIES):
        raise PaperAlertError(
            "paper alert policy.severities must define critical and warning"
        )
    severities: Dict[str, SeverityPolicy] = {}
    for severity in SEVERITIES:
        item = _object(
            severity_values[severity],
            f"paper alert policy.severities.{severity}",
        )
        item_fields = ("repeat_interval_seconds", "escalate_after_seconds")
        _expect_keys(
            item,
            required=item_fields,
            allowed=item_fields,
            path=f"paper alert policy.severities.{severity}",
        )
        severities[severity] = SeverityPolicy(
            repeat_interval_seconds=_integer(
                item["repeat_interval_seconds"],
                f"paper alert policy.severities.{severity}.repeat_interval_seconds",
                minimum=1,
            ),
            escalate_after_seconds=_integer(
                item["escalate_after_seconds"],
                f"paper alert policy.severities.{severity}.escalate_after_seconds",
            ),
        )
    return AlertPolicy(
        channel=channel,
        severities=severities,
        policy_sha256=_canonical_sha256(raw),
    )


def _validate_paper_audit_report(value: Mapping[str, Any]) -> Dict[str, Any]:
    report = _object(value, "paper audit report")
    fields = (
        "schema_version",
        "artifact_type",
        "as_of",
        "status",
        "manual_review_required",
        "automatic_execution_allowed",
        "paper_state_sha256",
        "audit_input_sha256",
        "thresholds",
        "risk_state",
        "summary",
        "alerts",
        "task_health",
        "open_orders",
        "execution_quality",
        "account_reconciliation",
        "report_sha256",
    )
    _expect_keys(report, required=fields, allowed=fields, path="paper audit report")
    if report["schema_version"] != AUDIT_REPORT_SCHEMA_VERSION:
        raise PaperAlertError("unsupported paper audit report schema")
    if report["artifact_type"] != "offline_paper_operational_audit":
        raise PaperAlertError("unexpected paper audit report artifact_type")
    _timestamp(report["as_of"], "paper audit report.as_of")
    if report["status"] not in {"pass", "review_required", "blocked"}:
        raise PaperAlertError("paper audit report.status is invalid")
    manual_review_required = _boolean(
        report["manual_review_required"],
        "paper audit report.manual_review_required",
    )
    if _boolean(
        report["automatic_execution_allowed"],
        "paper audit report.automatic_execution_allowed",
    ):
        raise PaperAlertError("paper audit report cannot allow automatic execution")
    expected_hash = _string(
        report["report_sha256"], "paper audit report.report_sha256"
    )
    payload = {key: value for key, value in report.items() if key != "report_sha256"}
    if expected_hash != _canonical_sha256(payload):
        raise PaperAlertError("paper audit report checksum is invalid")

    alert_ids = set()
    report_alerts = _array(report["alerts"], "paper audit report.alerts")
    for index, value in enumerate(report_alerts):
        path_prefix = f"paper audit report.alerts[{index}]"
        alert = _object(value, path_prefix)
        alert_fields = (
            "alert_id",
            "code",
            "severity",
            "subject",
            "message",
            "details",
        )
        _expect_keys(
            alert,
            required=alert_fields,
            allowed=alert_fields,
            path=path_prefix,
        )
        alert_id = _string(alert["alert_id"], f"{path_prefix}.alert_id")
        if alert_id in alert_ids:
            raise PaperAlertError("paper audit report has duplicate alert_id")
        alert_ids.add(alert_id)
        _string(alert["code"], f"{path_prefix}.code")
        if alert["severity"] not in SEVERITIES:
            raise PaperAlertError(f"{path_prefix}.severity is invalid")
        _string(alert["subject"], f"{path_prefix}.subject")
        _string(alert["message"], f"{path_prefix}.message")
        _object(alert["details"], f"{path_prefix}.details")
    summary = _object(report["summary"], "paper audit report.summary")
    alert_count = _integer(
        summary.get("alert_count"), "paper audit report.summary.alert_count"
    )
    critical_count = _integer(
        summary.get("critical_alert_count"),
        "paper audit report.summary.critical_alert_count",
    )
    warning_count = _integer(
        summary.get("warning_alert_count"),
        "paper audit report.summary.warning_alert_count",
    )
    actual_critical = sum(
        1 for item in report_alerts if item["severity"] == "critical"
    )
    actual_warning = sum(
        1 for item in report_alerts if item["severity"] == "warning"
    )
    if (
        alert_count != len(report_alerts)
        or critical_count != actual_critical
        or warning_count != actual_warning
    ):
        raise PaperAlertError("paper audit report alert summary is inconsistent")
    expected_status = (
        "blocked"
        if actual_critical
        else "review_required"
        if actual_warning
        else "pass"
    )
    if report["status"] != expected_status:
        raise PaperAlertError("paper audit report status is inconsistent")
    if manual_review_required != (expected_status != "pass"):
        raise PaperAlertError(
            "paper audit report manual_review_required is inconsistent"
        )
    return report


def load_paper_audit_report(path: Path) -> Dict[str, Any]:
    return _validate_paper_audit_report(
        _read_json(path, "paper audit report")
    )


def _validate_alert_actions(
    values: Sequence[Mapping[str, Any]], label: str
) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    action_ids = set()
    for index, value in enumerate(values):
        path_prefix = f"{label}[{index}]"
        action = _object(value, path_prefix)
        required = (
            "action_id",
            "alert_id",
            "action",
            "actor",
            "occurred_at",
            "reason",
        )
        allowed = required + ("silence_until",)
        _expect_keys(
            action,
            required=required,
            allowed=allowed,
            path=path_prefix,
        )
        action_id = _string(
            action["action_id"], f"{path_prefix}.action_id"
        )
        if action_id in action_ids:
            raise PaperAlertError("paper alert actions contain duplicate action_id")
        action_ids.add(action_id)
        _string(action["alert_id"], f"{path_prefix}.alert_id")
        if action["action"] not in ACTION_KINDS:
            raise PaperAlertError(f"{path_prefix}.action is invalid")
        _string(action["actor"], f"{path_prefix}.actor")
        _timestamp(action["occurred_at"], f"{path_prefix}.occurred_at")
        _string(action["reason"], f"{path_prefix}.reason")
        if action["action"] == "silence":
            if "silence_until" not in action:
                raise PaperAlertError("silence action requires silence_until")
            silence_until = _timestamp(
                action["silence_until"],
                f"{path_prefix}.silence_until",
            )
            occurred_at = _timestamp(
                action["occurred_at"],
                f"{path_prefix}.occurred_at",
            )
            if silence_until <= occurred_at:
                raise PaperAlertError("silence_until must be after occurred_at")
        elif "silence_until" in action:
            raise PaperAlertError(
                "silence_until is only allowed for a silence action"
            )
        actions.append(action)
    return actions


def load_alert_actions(path: Path) -> List[Dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise PaperAlertError(f"paper alert actions not found: {path}") from exc
    values: List[Mapping[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PaperAlertError(
                f"invalid paper alert action JSON at line {line_number}"
            ) from exc
        values.append(_object(value, f"paper alert action line {line_number}"))
    return _validate_alert_actions(values, "paper alert actions")


def _state_payload(state: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in state.items() if key != "state_sha256"}


def _with_state_hash(state: Mapping[str, Any]) -> Dict[str, Any]:
    payload = _state_payload(state)
    return {**payload, "state_sha256": _canonical_sha256(payload)}


def _validate_alert_record(value: Any, path: str) -> Dict[str, Any]:
    alert = _object(value, path)
    fields = (
        "alert_id",
        "code",
        "severity",
        "subject",
        "message",
        "details",
        "occurrence",
        "status",
        "first_seen",
        "last_seen",
        "last_report_sha256",
        "resolved_at",
        "acknowledged_at",
        "acknowledged_by",
        "acknowledgement_reason",
        "silenced_until",
        "silenced_by",
        "silence_reason",
        "delivery_count",
        "last_delivered_at",
        "last_delivery_level",
    )
    _expect_keys(alert, required=fields, allowed=fields, path=path)
    _string(alert["alert_id"], f"{path}.alert_id")
    _string(alert["code"], f"{path}.code")
    if alert["severity"] not in SEVERITIES:
        raise PaperAlertError(f"{path}.severity is invalid")
    _string(alert["subject"], f"{path}.subject")
    _string(alert["message"], f"{path}.message")
    _object(alert["details"], f"{path}.details")
    _integer(alert["occurrence"], f"{path}.occurrence", minimum=1)
    if alert["status"] not in ALERT_STATUSES:
        raise PaperAlertError(f"{path}.status is invalid")
    first_seen = _timestamp(alert["first_seen"], f"{path}.first_seen")
    last_seen = _timestamp(alert["last_seen"], f"{path}.last_seen")
    if last_seen < first_seen:
        raise PaperAlertError(f"{path}.last_seen precedes first_seen")
    _string(alert["last_report_sha256"], f"{path}.last_report_sha256")
    for field in (
        "resolved_at",
        "acknowledged_at",
        "silenced_until",
        "last_delivered_at",
    ):
        if alert[field] is not None:
            _timestamp(alert[field], f"{path}.{field}")
    for field in (
        "acknowledged_by",
        "acknowledgement_reason",
        "silenced_by",
        "silence_reason",
        "last_delivery_level",
    ):
        _optional_string(alert[field], f"{path}.{field}")
    if (
        alert["last_delivery_level"] is not None
        and alert["last_delivery_level"] not in DELIVERY_LEVELS
    ):
        raise PaperAlertError(f"{path}.last_delivery_level is invalid")
    _integer(alert["delivery_count"], f"{path}.delivery_count")
    if alert["resolved_at"] is not None and _timestamp(
        alert["resolved_at"], f"{path}.resolved_at"
    ) < first_seen:
        raise PaperAlertError(f"{path}.resolved_at precedes first_seen")
    if alert["acknowledged_at"] is None:
        if any(
            alert[field] is not None
            for field in ("acknowledged_by", "acknowledgement_reason")
        ):
            raise PaperAlertError(f"{path} has incomplete acknowledgement")
    elif any(
        alert[field] is None
        for field in ("acknowledged_by", "acknowledgement_reason")
    ):
        raise PaperAlertError(f"{path} has incomplete acknowledgement")
    if alert["silenced_until"] is None:
        if any(
            alert[field] is not None
            for field in ("silenced_by", "silence_reason")
        ):
            raise PaperAlertError(f"{path} has incomplete silence evidence")
    elif any(
        alert[field] is None for field in ("silenced_by", "silence_reason")
    ):
        raise PaperAlertError(f"{path} has incomplete silence evidence")
    delivery_count = alert["delivery_count"]
    delivery_evidence = (
        alert["last_delivered_at"],
        alert["last_delivery_level"],
    )
    if delivery_count == 0 and any(item is not None for item in delivery_evidence):
        raise PaperAlertError(f"{path} has delivery evidence without a delivery")
    if delivery_count > 0 and any(item is None for item in delivery_evidence):
        raise PaperAlertError(f"{path} is missing delivery evidence")
    return alert


def _validate_notification(value: Any, path: str) -> Dict[str, Any]:
    notification = _object(value, path)
    fields = (
        "notification_id",
        "alert_id",
        "occurrence",
        "sequence",
        "level",
        "channel",
        "created_at",
        "report_sha256",
        "payload",
    )
    _expect_keys(notification, required=fields, allowed=fields, path=path)
    _string(notification["notification_id"], f"{path}.notification_id")
    _string(notification["alert_id"], f"{path}.alert_id")
    _integer(notification["occurrence"], f"{path}.occurrence", minimum=1)
    _integer(notification["sequence"], f"{path}.sequence", minimum=1)
    if notification["level"] not in DELIVERY_LEVELS:
        raise PaperAlertError(f"{path}.level is invalid")
    if notification["channel"] != LOCAL_CHANNEL:
        raise PaperAlertError(f"{path}.channel is invalid")
    _timestamp(notification["created_at"], f"{path}.created_at")
    _string(notification["report_sha256"], f"{path}.report_sha256")
    _object(notification["payload"], f"{path}.payload")
    return notification


def _validate_receipt(value: Any, path: str) -> Dict[str, Any]:
    receipt = _object(value, path)
    fields = (
        "schema_version",
        "artifact_type",
        "notification_id",
        "alert_id",
        "occurrence",
        "level",
        "channel",
        "delivered_at",
        "notification_sha256",
        "receipt_sha256",
    )
    _expect_keys(receipt, required=fields, allowed=fields, path=path)
    if receipt["schema_version"] != DELIVERY_RECEIPT_SCHEMA_VERSION:
        raise PaperAlertError(f"{path}.schema_version is unsupported")
    if receipt["artifact_type"] != "offline_paper_alert_delivery_receipt":
        raise PaperAlertError(f"{path}.artifact_type is invalid")
    _string(receipt["notification_id"], f"{path}.notification_id")
    _string(receipt["alert_id"], f"{path}.alert_id")
    _integer(receipt["occurrence"], f"{path}.occurrence", minimum=1)
    if receipt["level"] not in DELIVERY_LEVELS:
        raise PaperAlertError(f"{path}.level is invalid")
    if receipt["channel"] != LOCAL_CHANNEL:
        raise PaperAlertError(f"{path}.channel is invalid")
    _timestamp(receipt["delivered_at"], f"{path}.delivered_at")
    _string(receipt["notification_sha256"], f"{path}.notification_sha256")
    expected = _string(receipt["receipt_sha256"], f"{path}.receipt_sha256")
    payload = {key: item for key, item in receipt.items() if key != "receipt_sha256"}
    if expected != _canonical_sha256(payload):
        raise PaperAlertError(f"{path} checksum is invalid")
    return receipt


def _validate_state(value: Mapping[str, Any]) -> Dict[str, Any]:
    state = dict(value)
    fields = (
        "schema_version",
        "artifact_type",
        "policy_sha256",
        "latest_report_sha256",
        "latest_report_as_of",
        "updated_at",
        "automatic_execution_allowed",
        "automatic_external_delivery_allowed",
        "alerts",
        "pending_notifications",
        "deliveries",
        "applied_actions",
        "state_sha256",
    )
    _expect_keys(state, required=fields, allowed=fields, path="paper alert state")
    if state["schema_version"] != ALERT_STATE_SCHEMA_VERSION:
        raise PaperAlertError("unsupported paper alert state schema")
    if state["artifact_type"] != "offline_paper_alert_state":
        raise PaperAlertError("unexpected paper alert state artifact_type")
    _string(state["policy_sha256"], "paper alert state.policy_sha256")
    _string(
        state["latest_report_sha256"],
        "paper alert state.latest_report_sha256",
    )
    _timestamp(
        state["latest_report_as_of"], "paper alert state.latest_report_as_of"
    )
    state_updated_at = _timestamp(
        state["updated_at"], "paper alert state.updated_at"
    )
    if _boolean(
        state["automatic_execution_allowed"],
        "paper alert state.automatic_execution_allowed",
    ):
        raise PaperAlertError("paper alert state cannot allow automatic execution")
    if _boolean(
        state["automatic_external_delivery_allowed"],
        "paper alert state.automatic_external_delivery_allowed",
    ):
        raise PaperAlertError("paper alert state cannot allow external delivery")
    expected = _string(state["state_sha256"], "paper alert state.state_sha256")
    if expected != _canonical_sha256(_state_payload(state)):
        raise PaperAlertError("paper alert state checksum is invalid")

    alerts_by_id: Dict[str, Dict[str, Any]] = {}
    for index, item in enumerate(_array(state["alerts"], "paper alert state.alerts")):
        alert = _validate_alert_record(item, f"paper alert state.alerts[{index}]")
        if alert["alert_id"] in alerts_by_id:
            raise PaperAlertError("paper alert state has duplicate alert_id")
        expected_status = "open"
        if alert["resolved_at"] is not None:
            expected_status = "resolved"
        elif alert["acknowledged_at"] is not None:
            expected_status = "acknowledged"
        elif (
            alert["silenced_until"] is not None
            and _timestamp(alert["silenced_until"], "alert.silenced_until")
            > state_updated_at
        ):
            expected_status = "silenced"
        if alert["status"] != expected_status:
            raise PaperAlertError("paper alert state status evidence is inconsistent")
        alerts_by_id[alert["alert_id"]] = alert
    notification_ids = set()
    for index, item in enumerate(
        _array(
            state["pending_notifications"],
            "paper alert state.pending_notifications",
        )
    ):
        notification = _validate_notification(
            item, f"paper alert state.pending_notifications[{index}]"
        )
        if notification["notification_id"] in notification_ids:
            raise PaperAlertError("paper alert state has duplicate notification_id")
        if notification["alert_id"] not in alerts_by_id:
            raise PaperAlertError("pending notification references unknown alert")
        alert = alerts_by_id[notification["alert_id"]]
        if notification["occurrence"] != alert["occurrence"]:
            raise PaperAlertError("pending notification occurrence is stale")
        if notification["sequence"] != alert["delivery_count"] + 1:
            raise PaperAlertError("pending notification sequence is invalid")
        if alert["status"] != "open":
            raise PaperAlertError("inactive alert has a pending notification")
        notification_ids.add(notification["notification_id"])
    delivery_ids = set()
    for index, item in enumerate(
        _array(state["deliveries"], "paper alert state.deliveries")
    ):
        receipt = _validate_receipt(item, f"paper alert state.deliveries[{index}]")
        if receipt["notification_id"] in delivery_ids:
            raise PaperAlertError("paper alert state has duplicate delivery receipt")
        if receipt["alert_id"] not in alerts_by_id:
            raise PaperAlertError("delivery receipt references unknown alert")
        if receipt["occurrence"] > alerts_by_id[receipt["alert_id"]]["occurrence"]:
            raise PaperAlertError("delivery receipt occurrence is from the future")
        delivery_ids.add(receipt["notification_id"])
    for alert_id, alert in alerts_by_id.items():
        current_receipts = [
            item
            for item in state["deliveries"]
            if item["alert_id"] == alert_id
            and item["occurrence"] == alert["occurrence"]
        ]
        if len(current_receipts) != alert["delivery_count"]:
            raise PaperAlertError("paper alert delivery count is inconsistent")
    action_ids = set()
    for index, item in enumerate(
        _array(state["applied_actions"], "paper alert state.applied_actions")
    ):
        action = _object(item, f"paper alert state.applied_actions[{index}]")
        action_fields = ("action_id", "action_sha256")
        _expect_keys(
            action,
            required=action_fields,
            allowed=action_fields,
            path=f"paper alert state.applied_actions[{index}]",
        )
        action_id = _string(
            action["action_id"],
            f"paper alert state.applied_actions[{index}].action_id",
        )
        _string(
            action["action_sha256"],
            f"paper alert state.applied_actions[{index}].action_sha256",
        )
        if action_id in action_ids:
            raise PaperAlertError("paper alert state has duplicate action_id")
        action_ids.add(action_id)
    return state


def load_alert_state(path: Path) -> Dict[str, Any]:
    return _validate_state(_read_json(path, "paper alert state"))


def save_alert_state(state: Mapping[str, Any], path: Path) -> None:
    validated = _validate_state(state)
    _write_json(validated, path)


def load_delivery_log(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    receipts: List[Dict[str, Any]] = []
    receipt_ids = set()
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PaperAlertError(
                f"invalid paper alert delivery log JSON at line {line_number}"
            ) from exc
        receipt = _validate_receipt(raw, f"delivery log line {line_number}")
        notification_id = receipt["notification_id"]
        if notification_id in receipt_ids:
            raise PaperAlertError("delivery log has duplicate notification_id")
        receipt_ids.add(notification_id)
        receipts.append(receipt)
    return receipts


def save_delivery_log(receipts: Sequence[Mapping[str, Any]], path: Path) -> None:
    validated = [
        _validate_receipt(item, f"delivery receipt[{index}]")
        for index, item in enumerate(receipts)
    ]
    notification_ids = [item["notification_id"] for item in validated]
    if len(notification_ids) != len(set(notification_ids)):
        raise PaperAlertError("delivery receipts contain duplicate notification_id")
    validated.sort(key=lambda item: (item["delivered_at"], item["notification_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    content = "".join(
        json.dumps(item, sort_keys=True, ensure_ascii=False) + "\n"
        for item in validated
    )
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _new_alert(
    alert: Mapping[str, Any], report_sha256: str, now: datetime, occurrence: int
) -> Dict[str, Any]:
    timestamp = now.isoformat()
    return {
        "alert_id": alert["alert_id"],
        "code": alert["code"],
        "severity": alert["severity"],
        "subject": alert["subject"],
        "message": alert["message"],
        "details": copy.deepcopy(alert["details"]),
        "occurrence": occurrence,
        "status": "open",
        "first_seen": timestamp,
        "last_seen": timestamp,
        "last_report_sha256": report_sha256,
        "resolved_at": None,
        "acknowledged_at": None,
        "acknowledged_by": None,
        "acknowledgement_reason": None,
        "silenced_until": None,
        "silenced_by": None,
        "silence_reason": None,
        "delivery_count": 0,
        "last_delivered_at": None,
        "last_delivery_level": None,
    }


def _refresh_status(alert: Dict[str, Any], now: datetime) -> None:
    if alert["resolved_at"] is not None:
        alert["status"] = "resolved"
    elif alert["acknowledged_at"] is not None:
        alert["status"] = "acknowledged"
    elif (
        alert["silenced_until"] is not None
        and _timestamp(alert["silenced_until"], "alert.silenced_until") > now
    ):
        alert["status"] = "silenced"
    else:
        alert["status"] = "open"


def _apply_actions(
    state: Dict[str, Any], actions: Sequence[Mapping[str, Any]], now: datetime
) -> None:
    alerts = {item["alert_id"]: item for item in state["alerts"]}
    applied = {
        item["action_id"]: item["action_sha256"]
        for item in state["applied_actions"]
    }
    state_updated_at = _timestamp(
        state["updated_at"], "paper alert state.updated_at"
    )
    sortable: List[Tuple[datetime, str, Mapping[str, Any]]] = []
    for action in actions:
        occurred_at = _timestamp(
            action["occurred_at"], "paper alert action.occurred_at"
        )
        sortable.append((occurred_at, action["action_id"], action))
    for occurred_at, action_id, action in sorted(sortable):
        action_sha256 = _canonical_sha256(action)
        if action_id in applied:
            if applied[action_id] != action_sha256:
                raise PaperAlertError("action_id was reused with different content")
            continue
        if occurred_at > now:
            raise PaperAlertError("paper alert action occurred_at is after now")
        if occurred_at < state_updated_at:
            raise PaperAlertError(
                "paper alert action occurred_at precedes the existing state"
            )
        alert_id = action["alert_id"]
        if alert_id not in alerts:
            raise PaperAlertError(
                f"paper alert action references unknown alert: {alert_id}"
            )
        alert = alerts[alert_id]
        if occurred_at < _timestamp(alert["first_seen"], "alert.first_seen"):
            raise PaperAlertError("paper alert action precedes the alert occurrence")
        if alert["resolved_at"] is not None:
            raise PaperAlertError("resolved alerts cannot receive operator actions")
        kind = action["action"]
        if kind == "acknowledge":
            if alert["acknowledged_at"] is not None:
                raise PaperAlertError("alert is already acknowledged")
            alert["acknowledged_at"] = occurred_at.isoformat()
            alert["acknowledged_by"] = action["actor"]
            alert["acknowledgement_reason"] = action["reason"]
        elif kind == "silence":
            if alert["acknowledged_at"] is not None:
                raise PaperAlertError("acknowledged alerts cannot be silenced")
            alert["silenced_until"] = _timestamp(
                action["silence_until"], "paper alert action.silence_until"
            ).isoformat()
            alert["silenced_by"] = action["actor"]
            alert["silence_reason"] = action["reason"]
        else:
            if alert["silenced_until"] is None:
                raise PaperAlertError("alert is not silenced")
            alert["silenced_until"] = None
            alert["silenced_by"] = None
            alert["silence_reason"] = None
        state["applied_actions"].append(
            {"action_id": action_id, "action_sha256": action_sha256}
        )
        applied[action_id] = action_sha256
        _refresh_status(alert, now)


def _notification_for(
    alert: Mapping[str, Any], report_sha256: str, policy: AlertPolicy, now: datetime
) -> Dict[str, Any]:
    severity_policy = policy.severities[alert["severity"]]
    first_seen = _timestamp(alert["first_seen"], "alert.first_seen")
    age_seconds = (now - first_seen).total_seconds()
    level = (
        "escalated"
        if age_seconds >= severity_policy.escalate_after_seconds
        else "standard"
    )
    sequence = alert["delivery_count"] + 1
    identity = {
        "alert_id": alert["alert_id"],
        "occurrence": alert["occurrence"],
        "sequence": sequence,
        "level": level,
        "channel": policy.channel,
    }
    return {
        "notification_id": _canonical_sha256(identity)[:24],
        **identity,
        "created_at": now.isoformat(),
        "report_sha256": report_sha256,
        "payload": {
            "code": alert["code"],
            "severity": alert["severity"],
            "subject": alert["subject"],
            "message": alert["message"],
            "details": copy.deepcopy(alert["details"]),
            "first_seen": alert["first_seen"],
            "last_seen": alert["last_seen"],
            "delivery_level": level,
            "manual_review_required": True,
            "automatic_execution_allowed": False,
        },
    }


def synchronize_paper_alerts(
    report: Mapping[str, Any],
    policy: AlertPolicy,
    now: datetime,
    *,
    previous_state: Optional[Mapping[str, Any]] = None,
    actions: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    now = _aware_timestamp(now, "now")
    report = _validate_paper_audit_report(report)
    actions = _validate_alert_actions(actions, "paper alert actions")
    report_as_of = _timestamp(report["as_of"], "paper audit report.as_of")
    if report_as_of > now:
        raise PaperAlertError("paper audit report.as_of is after now")
    report_sha256 = _string(
        report["report_sha256"], "paper audit report.report_sha256"
    )
    if previous_state is None:
        state: Dict[str, Any] = {
            "schema_version": ALERT_STATE_SCHEMA_VERSION,
            "artifact_type": "offline_paper_alert_state",
            "policy_sha256": policy.policy_sha256,
            "latest_report_sha256": report_sha256,
            "latest_report_as_of": report_as_of.isoformat(),
            "updated_at": now.isoformat(),
            "automatic_execution_allowed": False,
            "automatic_external_delivery_allowed": False,
            "alerts": [],
            "pending_notifications": [],
            "deliveries": [],
            "applied_actions": [],
        }
    else:
        state = copy.deepcopy(_validate_state(previous_state))
        if state["policy_sha256"] != policy.policy_sha256:
            raise PaperAlertError("paper alert policy changed for an existing state")
        previous_as_of = _timestamp(
            state["latest_report_as_of"], "paper alert state.latest_report_as_of"
        )
        if report_as_of < previous_as_of:
            raise PaperAlertError("paper audit report is older than the alert state")
        if (
            report_as_of == previous_as_of
            and report_sha256 != state["latest_report_sha256"]
        ):
            raise PaperAlertError(
                "paper audit report conflicts with the state at the same as_of"
            )
        if now < _timestamp(state["updated_at"], "paper alert state.updated_at"):
            raise PaperAlertError("now precedes the existing paper alert state")

    incoming = {item["alert_id"]: item for item in report["alerts"]}
    existing = {item["alert_id"]: item for item in state["alerts"]}
    report_changed = report_sha256 != state.get("latest_report_sha256")
    for alert_id in sorted(incoming):
        source = incoming[alert_id]
        if alert_id not in existing:
            item = _new_alert(source, report_sha256, now, 1)
            state["alerts"].append(item)
            existing[alert_id] = item
        elif existing[alert_id]["resolved_at"] is not None:
            occurrence = existing[alert_id]["occurrence"] + 1
            replacement = _new_alert(source, report_sha256, now, occurrence)
            index = state["alerts"].index(existing[alert_id])
            state["alerts"][index] = replacement
            existing[alert_id] = replacement
        else:
            item = existing[alert_id]
            item["code"] = source["code"]
            item["severity"] = source["severity"]
            item["subject"] = source["subject"]
            item["message"] = source["message"]
            item["details"] = copy.deepcopy(source["details"])
            if report_changed:
                item["last_seen"] = now.isoformat()
                item["last_report_sha256"] = report_sha256

    for alert_id, item in existing.items():
        if alert_id not in incoming and item["resolved_at"] is None:
            item["resolved_at"] = now.isoformat()
            item["status"] = "resolved"

    state["pending_notifications"] = [
        item
        for item in state["pending_notifications"]
        if existing[item["alert_id"]]["resolved_at"] is None
    ]
    _apply_actions(state, actions, now)
    for item in state["alerts"]:
        _refresh_status(item, now)
    state["pending_notifications"] = [
        item
        for item in state["pending_notifications"]
        if existing[item["alert_id"]]["status"] == "open"
    ]

    pending_alert_ids = {
        item["alert_id"] for item in state["pending_notifications"]
    }
    for item in sorted(state["alerts"], key=lambda value: value["alert_id"]):
        if item["status"] != "open" or item["alert_id"] in pending_alert_ids:
            continue
        due = item["delivery_count"] == 0
        if item["last_delivered_at"] is not None:
            interval = policy.severities[item["severity"]].repeat_interval_seconds
            due_at = _timestamp(
                item["last_delivered_at"], "alert.last_delivered_at"
            ) + timedelta(seconds=interval)
            due = now >= due_at
        if due:
            state["pending_notifications"].append(
                _notification_for(item, report_sha256, policy, now)
            )

    state["alerts"].sort(key=lambda item: item["alert_id"])
    state["pending_notifications"].sort(
        key=lambda item: item["notification_id"]
    )
    state["applied_actions"].sort(key=lambda item: item["action_id"])
    state["latest_report_sha256"] = report_sha256
    state["latest_report_as_of"] = report_as_of.isoformat()
    state["updated_at"] = now.isoformat()
    return _validate_state(_with_state_hash(state))


def dispatch_local_alerts(
    state: Mapping[str, Any],
    existing_receipts: Sequence[Mapping[str, Any]],
    delivered_at: datetime,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    delivered_at = _aware_timestamp(delivered_at, "delivered_at")
    updated = copy.deepcopy(_validate_state(state))
    if delivered_at < _timestamp(updated["updated_at"], "paper alert state.updated_at"):
        raise PaperAlertError("delivered_at precedes the paper alert state")
    receipts: Dict[str, Dict[str, Any]] = {}
    state_receipts = {
        item["notification_id"]: item for item in updated["deliveries"]
    }
    pending = {
        item["notification_id"]: item
        for item in updated["pending_notifications"]
    }
    allowed_receipt_ids = set(state_receipts) | set(pending)
    for index, value in enumerate(existing_receipts):
        receipt = _validate_receipt(value, f"existing delivery receipt[{index}]")
        notification_id = receipt["notification_id"]
        if notification_id in receipts:
            raise PaperAlertError("delivery receipts contain duplicate notification_id")
        if notification_id not in allowed_receipt_ids:
            raise PaperAlertError(
                "delivery log contains a receipt from another alert state"
            )
        if (
            notification_id in state_receipts
            and receipt != state_receipts[notification_id]
        ):
            raise PaperAlertError(
                "delivery log conflicts with the paper alert state"
            )
        receipts[notification_id] = receipt
    missing_receipts = sorted(set(state_receipts) - set(receipts))
    if missing_receipts:
        raise PaperAlertError(
            "delivery log is missing receipts recorded by the alert state"
        )

    alerts = {item["alert_id"]: item for item in updated["alerts"]}
    for notification in updated["pending_notifications"]:
        notification_id = notification["notification_id"]
        notification_sha256 = _canonical_sha256(notification)
        if notification_id in receipts:
            receipt = receipts[notification_id]
            if receipt["notification_sha256"] != notification_sha256:
                raise PaperAlertError(
                    "delivery log notification conflicts with pending content"
                )
        else:
            payload = {
                "schema_version": DELIVERY_RECEIPT_SCHEMA_VERSION,
                "artifact_type": "offline_paper_alert_delivery_receipt",
                "notification_id": notification_id,
                "alert_id": notification["alert_id"],
                "occurrence": notification["occurrence"],
                "level": notification["level"],
                "channel": notification["channel"],
                "delivered_at": delivered_at.isoformat(),
                "notification_sha256": notification_sha256,
            }
            receipt = {**payload, "receipt_sha256": _canonical_sha256(payload)}
            receipts[notification_id] = receipt
        alert = alerts[notification["alert_id"]]
        if alert["occurrence"] != notification["occurrence"]:
            raise PaperAlertError("pending notification occurrence is stale")
        if alert["delivery_count"] + 1 != notification["sequence"]:
            raise PaperAlertError("pending notification sequence is invalid")
        alert["delivery_count"] = notification["sequence"]
        alert["last_delivered_at"] = receipt["delivered_at"]
        alert["last_delivery_level"] = notification["level"]

    updated["pending_notifications"] = []
    merged = sorted(
        receipts.values(),
        key=lambda item: (item["delivered_at"], item["notification_id"]),
    )
    updated["deliveries"] = copy.deepcopy(merged)
    updated["updated_at"] = delivered_at.isoformat()
    return _validate_state(_with_state_hash(updated)), merged
