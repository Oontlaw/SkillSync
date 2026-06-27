"""
SkillSync — Slack Notification Service
Sends structured notifications to a Slack incoming webhook.
All functions are fire-and-forget — failures are logged but never raised.
Configure via SLACK_WEBHOOK_URL in .env. If not set, all calls no-op silently.
"""

import json
import logging
import os
from datetime import datetime

import requests

logger = logging.getLogger("skillsync.slack")

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
SLACK_TIMEOUT = 3  # seconds — never block the main thread long
BASE_URL = os.environ.get("SKILLSYNC_PUBLIC_URL", "http://localhost:5000")


def _enabled():
    return bool(SLACK_WEBHOOK_URL)


def _send(payload: dict) -> bool:
    """POST payload to Slack webhook. Returns True on success."""
    if not _enabled():
        return False
    try:
        resp = requests.post(
            SLACK_WEBHOOK_URL,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=SLACK_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning(f"Slack webhook returned {resp.status_code}: {resp.text}")
            return False
        return True
    except Exception as e:
        logger.warning(f"Slack notification failed (non-critical): {e}")
        return False


def _workspace_url(path: str) -> str:
    return f"{BASE_URL}/workspace{path}"


def _ts() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")


# ─────────────────────────────────────────────────────────
# WORK ENGINE NOTIFICATIONS
# ─────────────────────────────────────────────────────────


def notify_task_completed(
    worker_name: str, task_title: str, pts: float, on_time: bool, worker_id: int
) -> bool:
    """Sent when HR marks a task as completed."""
    emoji = "✅"
    timing = "on time" if on_time else "late"
    pts_str = f"+{int(pts)}" if pts >= 0 else str(int(pts))
    return _send(
        {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"{emoji} *Task Completed* ({timing})\n"
                            f"*Worker:* {worker_name}\n"
                            f"*Task:* {task_title}\n"
                            f"*Points:* {pts_str} pts"
                        ),
                    },
                    "accessory": {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View Worker"},
                        "url": _workspace_url(f"/workers/{worker_id}/summary"),
                    },
                },
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"SkillSync · {_ts()}"}],
                },
            ]
        }
    )


def notify_task_missed(worker_name: str, task_title: str, worker_id: int) -> bool:
    """Sent when HR marks a task as missed."""
    return _send(
        {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"❌ *Task Missed* — needs attention\n"
                            f"*Worker:* {worker_name}\n"
                            f"*Task:* {task_title}\n"
                            f"*Points:* -15 pts"
                        ),
                    },
                    "accessory": {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Review Worker"},
                        "url": _workspace_url(f"/workers/{worker_id}/summary"),
                    },
                },
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"SkillSync · {_ts()}"}],
                },
            ]
        }
    )


def notify_task_overdue(
    worker_name: str, task_title: str, days_overdue: int, worker_id: int
) -> bool:
    """Sent by background task when a pending task passes its due_at."""
    return _send(
        {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"⏰ *Task Overdue* — {days_overdue} day(s)\n"
                            f"*Worker:* {worker_name}\n"
                            f"*Task:* {task_title}\n"
                            f"Action needed: mark complete or missed"
                        ),
                    },
                    "accessory": {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Take Action"},
                        "url": _workspace_url(f"/workers/{worker_id}"),
                    },
                },
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"SkillSync · {_ts()}"}],
                },
            ]
        }
    )


# ─────────────────────────────────────────────────────────
# ML NOTIFICATIONS
# ─────────────────────────────────────────────────────────


def notify_burnout_flagged(
    worker_name: str, burnout_score: int, signals: list, worker_id: int
) -> bool:
    """Sent when burnout scan flags a worker."""
    signals_str = " · ".join(signals) if signals else "Multiple signals"
    severity = "🔴 High" if burnout_score > 60 else "🟡 Medium"
    return _send(
        {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"🔥 *Burnout Risk Detected* — {severity}\n"
                            f"*Worker:* {worker_name}\n"
                            f"*Risk Score:* {burnout_score}%\n"
                            f"*Signals:* {signals_str}"
                        ),
                    },
                    "accessory": {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View Burnout Report"},
                        "url": _workspace_url(f"/workers/{worker_id}/summary"),
                    },
                },
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"SkillSync · {_ts()}"}],
                },
            ]
        }
    )


def notify_anomaly_detected(
    worker_name: str, anomaly_type: str, severity: int, worker_id: int
) -> bool:
    """Sent when ML flags a behavioral anomaly for a linked workspace worker."""
    emoji = "🔴" if severity >= 70 else "🟡" if severity >= 40 else "⚪"
    return _send(
        {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"{emoji} *Behavioral Anomaly Detected*\n"
                            f"*Worker:* {worker_name}\n"
                            f"*Type:* {anomaly_type.replace('_', ' ').title()}\n"
                            f"*Severity:* {severity}/100"
                        ),
                    },
                    "accessory": {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Review Anomaly"},
                        "url": _workspace_url("/overrides"),
                    },
                },
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"SkillSync · {_ts()}"}],
                },
            ]
        }
    )


def notify_model_drift(drift_reasons: list) -> bool:
    """Sent when get_model_health() detects drift."""
    reasons_str = "\n".join(f"• {r}" for r in drift_reasons)
    return _send(
        {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"⚠️ *ML Model Drift Detected*\n"
                            f"One or more models are underperforming:\n"
                            f"{reasons_str}"
                        ),
                    },
                    "accessory": {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Retrain Models"},
                        "url": _workspace_url("/overrides"),
                    },
                },
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"SkillSync · {_ts()}"}],
                },
            ]
        }
    )


# ─────────────────────────────────────────────────────────
# WORKSPACE EVENTS
# ─────────────────────────────────────────────────────────


def notify_score_corrected(
    worker_name: str,
    original: float,
    corrected: float,
    reason: str,
    corrected_by: str,
    worker_id: int,
) -> bool:
    """Sent when HR manually corrects a score via overrides panel."""
    delta = corrected - original
    delta_str = f"{'+' if delta >= 0 else ''}{int(delta)}"
    return _send(
        {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"📝 *Score Corrected by HR*\n"
                            f"*Worker:* {worker_name}\n"
                            f"*Change:* {int(original)} → {int(corrected)} "
                            f"({delta_str} pts)\n"
                            f"*Reason:* {reason or 'No reason given'}\n"
                            f"*By:* {corrected_by}"
                        ),
                    },
                    "accessory": {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View Worker"},
                        "url": _workspace_url(f"/workers/{worker_id}/summary"),
                    },
                },
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"SkillSync · {_ts()}"}],
                },
            ]
        }
    )


def notify_worker_linked(
    worker_name: str, discord_id: str, org_name: str, worker_id: int
) -> bool:
    """Sent when a WorkerIdentity is created linking Discord to workspace."""
    return _send(
        {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"🔗 *Worker Identity Linked*\n"
                            f"*Worker:* {worker_name}\n"
                            f"*Discord ID:* {discord_id}\n"
                            f"*Organisation:* {org_name}\n"
                            f"Community signals now available for this worker."
                        ),
                    },
                    "accessory": {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View Identities"},
                        "url": _workspace_url("/identities"),
                    },
                },
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"SkillSync · {_ts()}"}],
                },
            ]
        }
    )


def notify_team_health_summary(
    org_name: str, green: int, yellow: int, red: int
) -> bool:
    """Weekly team health digest — called by background task."""
    total = green + yellow + red
    if total == 0:
        return False
    return _send(
        {
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"📊 Weekly Team Health — {org_name}",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"🟢 *On Track*\n{green} workers"},
                        {
                            "type": "mrkdwn",
                            "text": f"🟡 *Needs Attention*\n{yellow} workers",
                        },
                        {"type": "mrkdwn", "text": f"🔴 *At Risk*\n{red} workers"},
                        {"type": "mrkdwn", "text": f"👥 *Total*\n{total} workers"},
                    ],
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "View Team Health"},
                            "url": _workspace_url("/team-health"),
                            "style": "primary" if red > 0 else "default",
                        }
                    ],
                },
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"SkillSync · {_ts()}"}],
                },
            ]
        }
    )
