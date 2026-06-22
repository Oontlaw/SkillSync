import os
import requests
from datetime import datetime, timedelta

JIRA_URL = os.getenv('JIRA_URL', '')
JIRA_EMAIL = os.getenv('JIRA_EMAIL', '')
JIRA_API_TOKEN = os.getenv('JIRA_API_TOKEN', '')
JIRA_PROJECT = os.getenv('JIRA_PROJECT', '')
JIRA_JQL = os.getenv('JIRA_JQL', f'project={JIRA_PROJECT}')

STATUS_MAP = {
    'Done': 'completed',
    'Closed': 'completed',
    'Resolved': 'completed',
    'In Progress': 'pending',
    'In Review': 'pending',
    'Open': 'pending',
    'To Do': 'pending',
    'Canceled': 'missed',
    'Rejected': 'missed',
}

PRIORITY_MAP = {
    'Highest': 'critical',
    'High': 'high',
    'Medium': 'medium',
    'Low': 'low',
    'Lowest': 'low',
}

POINTS_MAP = {
    'completed': ('task_completed_on_time', True),
    'missed': ('task_missed', False),
}


def is_configured():
    return bool(JIRA_URL and JIRA_EMAIL and JIRA_API_TOKEN and JIRA_PROJECT)


def _jira_auth():
    return (JIRA_EMAIL, JIRA_API_TOKEN)


def poll_issues(days_back=7):
    """Fetch issues updated in the last N days from Jira.
    Returns list of dicts with issue data."""
    if not is_configured():
        return []
    since = (datetime.utcnow() - timedelta(days=days_back)).strftime('%Y-%m-%d %H:%M')
    jql = f'{JIRA_JQL} AND updated >= "{since}"'
    url = f'{JIRA_URL.rstrip("/")}/rest/api/3/search'
    try:
        resp = requests.get(url, auth=_jira_auth(), params={
            'jql': jql,
            'fields': 'summary,status,assignee,priority,updated,duedate,description',
            'maxResults': 50,
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        issues = []
        for issue in data.get('issues', []):
            fields = issue.get('fields', {})
            assignee = fields.get('assignee', {}) or {}
            issues.append({
                'id': issue['id'],
                'key': issue['key'],
                'self': issue.get('self', ''),
                'summary': fields.get('summary', ''),
                'description': (fields.get('description') or {}).get('content', [{}])[0].get('content', [{}])[0].get('text', '') if isinstance(fields.get('description'), dict) else str(fields.get('description', '')),
                'status': (fields.get('status') or {}).get('name', 'Unknown'),
                'priority': (fields.get('priority') or {}).get('name', 'Medium'),
                'assignee_email': assignee.get('emailAddress', ''),
                'assignee_display': assignee.get('displayName', ''),
                'assignee_account_id': assignee.get('accountId', ''),
                'updated': fields.get('updated', ''),
                'due_date': fields.get('duedate', ''),
            })
        return issues
    except Exception as e:
        print(f'[JiraConnector] Poll error: {e}')
        return []


def map_issue_to_task(issue, worker_id):
    """Convert a Jira issue dict to Task model kwargs."""
    status_raw = issue.get('status', 'Unknown')
    status = STATUS_MAP.get(status_raw, 'pending')
    priority_raw = issue.get('priority', 'Medium')
    priority = PRIORITY_MAP.get(priority_raw, 'medium')
    due = None
    if issue.get('due_date'):
        try:
            due = datetime.strptime(issue['due_date'], '%Y-%m-%d').isoformat()
        except ValueError:
            pass
    return {
        'worker_id': worker_id,
        'title': f'[{issue["key"]}] {issue["summary"]}',
        'description': issue.get('description', ''),
        'status': status,
        'source': 'jira',
        'external_id': issue['key'],
        'external_url': issue.get('self', ''),
        'priority': priority,
        'due_at': due,
    }
