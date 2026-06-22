from datetime import datetime

STATUS_MAP = {
    'completed': 'completed',
    'success': 'completed',
    'done': 'completed',
    'closed': 'completed',
    'failed': 'missed',
    'missed': 'missed',
    'canceled': 'missed',
    'cancelled': 'missed',
    'rejected': 'missed',
    'in_progress': 'pending',
    'in progress': 'pending',
    'pending': 'pending',
    'open': 'pending',
    'todo': 'pending',
}

PRIORITY_MAP = {
    'critical': 'critical',
    'urgent': 'critical',
    'high': 'high',
    'medium': 'medium',
    'normal': 'medium',
    'low': 'low',
    'lowest': 'low',
}


def parse_webhook_payload(data):
    """Parse a generic webhook payload into normalized task data.
    Expected schema (all fields optional, best-effort mapping):
      {
        "event": "task_completed" | "task_created" | "task_updated",
        "task_id": "EXT-123",
        "title": "Fix login bug",
        "description": "...",
        "status": "completed" | "pending" | "missed",
        "priority": "high" | "medium" | "low",
        "assignee_email": "user@company.com",
        "assignee_discord_id": "123456789",
        "due_date": "2026-07-01",
        "url": "https://..."
      }
    Returns dict with normalized fields or None if unparseable.
    """
    task_id = data.get('task_id') or data.get('id') or data.get('key', '')
    title = data.get('title') or data.get('summary', '')
    if not title and task_id:
        title = f'Task {task_id}'
    status_raw = (data.get('status') or 'pending').lower()
    status = STATUS_MAP.get(status_raw, 'pending')
    priority_raw = (data.get('priority') or 'medium').lower()
    priority = PRIORITY_MAP.get(priority_raw, 'medium')
    due = None
    raw_due = data.get('due_date') or data.get('due_at') or data.get('duedate')
    if raw_due:
        try:
            due = datetime.fromisoformat(raw_due.replace('Z', '+00:00')).isoformat()
        except (ValueError, AttributeError):
            try:
                due = datetime.strptime(str(raw_due)[:10], '%Y-%m-%d').isoformat()
            except ValueError:
                pass
    description = data.get('description') or data.get('body', '')
    url = data.get('url') or data.get('external_url') or data.get('self', '')
    assignee_email = data.get('assignee_email', '')
    assignee_discord_id = data.get('assignee_discord_id') or data.get('discord_id', '')

    return {
        'external_id': str(task_id),
        'title': title,
        'description': str(description),
        'status': status,
        'priority': priority,
        'due_at': due,
        'external_url': url,
        'source': data.get('source', 'webhook'),
        'assignee_email': assignee_email,
        'assignee_discord_id': str(assignee_discord_id) if assignee_discord_id else '',
    }
