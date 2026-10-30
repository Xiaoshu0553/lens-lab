"""Optional presentation times; recorded clocks and model evidence stay intact."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json

from .translation_purpose import DEMONSTRATION_PURPOSE, record_purpose


def _date(value):
    date = datetime.fromisoformat(value)
    if date.tzinfo is None:
        raise ValueError('A presentation time must specify its timezone')
    return date


class PresentationTimeline:
    def __init__(self, settings):
        self.enabled = settings.translation_showcase_mode is True
        self.path = settings.database_path.parent / '.translation-presentation-times.json'

    def snapshot(self):
        if not self.enabled:
            return {}
        try:
            payload = json.loads(self.path.read_text())
            if payload.get('version') != 1 or not isinstance(payload.get('records'), dict):
                return {}
            return payload['records']
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def project(self, row, mapping=None):
        """Pure output projection. Never pass its return value to persistence."""
        if not self.enabled or row.get('record_kind') != 'participant' or not row.get('ended_at'):
            return row
        if record_purpose(row) != DEMONSTRATION_PURPOSE or row.get('pending') or row.get('evaluation_pending'):
            return row
        entry = (self.snapshot() if mapping is None else mapping).get(row['id'])
        if not isinstance(entry, dict):
            return row
        if (entry.get('recorded_created_at') != row.get('created_at')
                or entry.get('recorded_ended_at') != row.get('ended_at')
                or entry.get('submission_sha256') != hashlib.sha256((row.get('submission') or '').encode()).hexdigest()):
            return row
        try:
            recorded_start, recorded_end = _date(row['created_at']), _date(row['ended_at'])
            start = _date(entry['created_at'])
            if recorded_end < recorded_start:
                return row
            offset = start - recorded_start
            end = recorded_end + offset
            assessment = row.get('assessment') or {}
            if assessment.get('evaluated_at') and end > _date(assessment['evaluated_at']):
                return row
            projected = deepcopy(row)
            projected['created_at'], projected['ended_at'] = start.isoformat(), end.isoformat()
            if row.get('draft_saved_at'):
                projected['draft_saved_at'] = (_date(row['draft_saved_at']) + offset).isoformat()
            for message in projected.get('messages', []):
                if message.get('created_at'):
                    message['created_at'] = (_date(message['created_at']) + offset).isoformat()
            return projected
        except (ValueError, KeyError, TypeError, OverflowError):
            return row
