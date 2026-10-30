#!/usr/bin/env python3
"""Evaluate explicitly selected submitted records through the authenticated API.

This runner never creates a translation, calculates a score, changes a model
configuration or rewrites a submission. The server supplies every assessment.
One manifest binds one cohort, evaluation batch and immutable submission hashes.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from app.config import load_dotenv

VERSION = 'translation-evaluation-http-v1'
LOCAL = {'localhost', '127.0.0.1', '::1'}
MAX_POST_ATTEMPTS = 3  # Initial request and no more than two recoverable retries.


class EvaluationError(RuntimeError):
    """Safe diagnostics without credentials, HTTP bodies or submission text."""


class APIError(EvaluationError):
    def __init__(self, method, path, status):
        self.status = status
        super().__init__(f'{method} {path}: ' + (f'HTTP {status}' if status else 'network timeout or failure'))


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def submission_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def normalize_origin(value):
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise EvaluationError('Invalid platform origin') from exc
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.path not in {'', '/'} or parsed.query or parsed.fragment
            or (parsed.scheme == 'http' and parsed.hostname.lower() not in LOCAL)):
        raise EvaluationError('Use a plain HTTPS origin; loopback HTTP is allowed, credentials and paths are not')
    host = parsed.hostname.lower()
    if ':' in host:
        host = '[' + host + ']'
    return f'{parsed.scheme}://{host}' + (f':{port}' if port else '')


def load_ids(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    ids = value.get('ids') if isinstance(value, dict) else value
    if (not isinstance(ids, list) or not ids or any(not isinstance(item, str)
            or re.fullmatch(r'[A-Za-z0-9_-]{1,100}', item) is None for item in ids)
            or len(set(ids)) != len(ids)):
        raise EvaluationError('IDs file must contain an explicit nonempty list of unique record IDs')
    return sorted(ids)


def save_manifest(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.translation-evaluation-', dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


@contextmanager
def manifest_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise EvaluationError('Another evaluator is using this manifest') from exc
        yield
    finally:
        os.close(descriptor)


class Platform:
    def __init__(self, base_url, password, *, request_timeout=150, transport=None):
        self.base_url = normalize_origin(base_url)
        self.client = httpx.Client(base_url=self.base_url, auth=('admin', password),
            timeout=httpx.Timeout(request_timeout, connect=15), follow_redirects=False,
            trust_env=urlsplit(self.base_url).hostname not in LOCAL, transport=transport)

    def close(self):
        self.client.close()

    def request(self, method, path, payload=None):
        try:
            response = self.client.request(method, path, json=payload, headers={'Cache-Control': 'no-store'})
        except httpx.RequestError as exc:
            raise APIError(method, path, None) from exc
        if not response.is_success:
            raise APIError(method, path, response.status_code)
        try:
            value = response.json()
        except ValueError as exc:
            raise EvaluationError('Platform response was not JSON') from exc
        if not isinstance(value, dict):
            raise EvaluationError('Platform response was not an object')
        return value


def get_record(api, sid):
    value = api.request('GET', '/api/admin/translation/records/' + quote(sid, safe=''))
    record = value.get('record', value)
    if not isinstance(record, dict):
        raise EvaluationError('Invalid record response')
    return record


def ensure_record(record, cohort, expected_hash=None):
    if (record.get('record_kind') != 'participant' or record.get('cohort_id') != cohort
            or record.get('current_cohort') is False or not record.get('ended_at')
            or not isinstance(record.get('submission'), str) or not record['submission'].strip()):
        raise EvaluationError('Selected record is not a submitted current-cohort translation session')
    digest = submission_hash(record['submission'])
    if expected_hash is not None and digest != expected_hash:
        raise EvaluationError('The immutable submission hash changed; stop this manifest')
    return digest


def check_summary(api, manifest=None, cohort=None):
    summary = api.request('GET', '/api/admin/translation/summary')
    if not summary.get('panel_ready') or not summary.get('evaluation_batch_id') or not summary.get('cohort_id'):
        raise EvaluationError('No complete current evaluation batch is configured')
    if summary.get('study_mode') == 'demo':
        raise EvaluationError('Rule-only runtime does not support model evaluation')
    if cohort is not None and summary['cohort_id'] != cohort:
        raise EvaluationError('The expected cohort differs from the current cohort')
    if manifest and (summary['evaluation_batch_id'] != manifest['evaluation_batch_id']
            or summary['cohort_id'] != manifest['cohort_id']
            or summary.get('evaluation_mode', 'distinct_models') != manifest['evaluation_mode']):
        raise EvaluationError('The evaluation batch or cohort changed; retain this manifest and stop')
    return summary


def assess_current(record, batch):
    candidates = [record.get('active_assessment'), record.get('assessment'), *(record.get('assessment_history') or [])]
    return next((assessment for assessment in candidates if isinstance(assessment, dict)
                 and assessment.get('evaluation_batch_id') == batch), None)


def evaluate_records(api, ids, manifest_path, *, cohort=None, allow_reassessment=False,
                     poll_seconds=2, pending_timeout=650, sleep=time.sleep):
    """Sequential execution is intentional: every next POST rechecks the batch."""
    path = Path(manifest_path)
    ids = sorted(ids)
    if not ids or len(set(ids)) != len(ids):
        raise EvaluationError('Select explicit unique IDs')
    initial = check_summary(api, cohort=cohort)
    expected = dict(version=VERSION, base_url=api.base_url, ids=ids, cohort_id=initial['cohort_id'],
        evaluation_batch_id=initial['evaluation_batch_id'], evaluation_mode=initial.get('evaluation_mode', 'distinct_models'),
        allow_reassessment=bool(allow_reassessment))
    with manifest_lock(path):
        if path.exists():
            manifest = json.loads(path.read_text(encoding='utf-8'))
            if any(manifest.get(key) != value for key, value in expected.items()):
                raise EvaluationError('Manifest origin, selected IDs, cohort, batch or reassessment permission changed')
            if not isinstance(manifest.get('records'), list) or [item.get('id') for item in manifest['records']] != ids:
                raise EvaluationError('Manifest records are damaged')
        else:
            # Validate every target before the first paid model request.
            entries = []
            for sid in ids:
                record = get_record(api, sid)
                digest = ensure_record(record, initial['cohort_id'])
                if record.get('assessment') and not assess_current(record, initial['evaluation_batch_id']) and not allow_reassessment:
                    raise EvaluationError('Existing other-batch assessment requires explicit --allow-reassessment')
                entries.append(dict(id=sid, submission_sha256=digest, status='planned', post_attempts=0))
            manifest = dict(**expected, created_at=utc_now(), status='running', records=entries)
            save_manifest(path, manifest)
        persist = lambda: save_manifest(path, manifest)

        def settled(entry):
            deadline = time.monotonic() + pending_timeout
            while True:
                check_summary(api, manifest)
                record = get_record(api, entry['id'])
                ensure_record(record, manifest['cohort_id'], entry['submission_sha256'])
                if not record.get('evaluation_pending'):
                    return record
                entry.update(status='pending', last_synced_at=utc_now()); persist()
                if time.monotonic() >= deadline:
                    raise EvaluationError('Accepted evaluation remains pending; resume this manifest later')
                sleep(poll_seconds)

        def complete(entry, assessment):
            if assessment.get('evaluation_batch_id') != manifest['evaluation_batch_id']:
                raise EvaluationError('Returned assessment belongs to another batch')
            digest = submission_hash(json.dumps(assessment, ensure_ascii=False, sort_keys=True, allow_nan=False))
            if entry.get('assessment_sha256') not in (None, digest):
                raise EvaluationError('A previously completed same-batch assessment changed')
            entry.update(status='completed', assessment_sha256=digest,
                         assessment_batch_id=assessment['evaluation_batch_id'], completed_at=entry.get('completed_at') or utc_now())
            entry.pop('last_error', None); persist()

        try:
            manifest['status'] = 'running'; persist()
            for entry in manifest['records']:
                while True:
                    record = settled(entry)
                    assessment = assess_current(record, manifest['evaluation_batch_id'])
                    if assessment:
                        complete(entry, assessment); break
                    if record.get('assessment') and not allow_reassessment:
                        raise EvaluationError('Existing other-batch assessment requires explicit reassessment permission')
                    if entry.get('post_attempts', 0) >= MAX_POST_ATTEMPTS:
                        raise EvaluationError('At most two retries are allowed; retry budget exhausted for this record')
                    entry.update(status='requesting', post_attempts=entry.get('post_attempts', 0) + 1)
                    persist()  # Persist the budget and immutable guards before POST.
                    body = dict(evaluation_batch_id=manifest['evaluation_batch_id'],
                        submission_sha256=entry['submission_sha256'], allow_reassessment=bool(allow_reassessment))
                    try:
                        assessment = api.request('POST', '/api/admin/translation/records/' + quote(entry['id'], safe='') + '/evaluate', body)
                    except APIError as exc:
                        entry['last_error'] = dict(http_status=exc.status, at=utc_now()); persist()
                        if exc.status not in {None, 409, 502}:
                            raise  # Includes 401/403; unknown failures never retry.
                        recovered = settled(entry)  # A timeout may have committed.
                        assessment = assess_current(recovered, manifest['evaluation_batch_id'])
                        if assessment:
                            complete(entry, assessment); break
                        if exc.status == 409:
                            raise EvaluationError('Evaluation was rejected by a batch/source/state guard; stop') from exc
                        if entry['post_attempts'] >= MAX_POST_ATTEMPTS:
                            raise EvaluationError('At most two retries are allowed; retry budget exhausted') from exc
                        sleep(entry['post_attempts']); continue
                    complete(entry, assessment)
                    # If a configuration changed during an accepted call, preserve
                    # that legitimate result and stop before another paid request.
                    check_summary(api, manifest)
                    break
            check_summary(api, manifest)
            manifest.update(status='completed', completed_at=utc_now()); persist()
            return manifest
        except Exception as exc:
            manifest.update(status='stopped', last_error=str(exc) if isinstance(exc, EvaluationError) else type(exc).__name__)
            persist()
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--ids-file', required=True, type=Path, help='JSON list of exact IDs or {"ids":[...]}')
    parser.add_argument('--cohort')
    parser.add_argument('--allow-reassessment', action='store_true', help='Explicitly create a new-batch assessment while retaining prior results')
    parser.add_argument('--workers', type=int, choices=[1], default=1, help='Sequential record evaluation, one worker')
    parser.add_argument('--request-timeout', type=float, default=150)
    parser.add_argument('--poll-seconds', type=float, default=2)
    args = parser.parse_args(argv)
    platform = None
    try:
        if not (math.isfinite(args.request_timeout) and 1 <= args.request_timeout <= 600
                and math.isfinite(args.poll_seconds) and .1 <= args.poll_seconds <= 30):
            raise EvaluationError('Invalid timeout or polling interval')
        path = args.manifest.resolve()
        if (ROOT / 'static').resolve() in path.parents:
            raise EvaluationError('The private evaluation manifest cannot be stored under static/')
        load_dotenv()
        password = os.getenv('TRANSLATION_EVALUATION_ADMIN_PASSWORD') or os.getenv('ADMIN_PASSWORD', '')
        if not password:
            raise EvaluationError('Set ADMIN_PASSWORD or TRANSLATION_EVALUATION_ADMIN_PASSWORD locally')
        platform = Platform(args.base_url, password, request_timeout=args.request_timeout)
        result = evaluate_records(platform, load_ids(args.ids_file), path, cohort=args.cohort,
            allow_reassessment=args.allow_reassessment, poll_seconds=args.poll_seconds)
        print(json.dumps(dict(status=result['status'], records=len(result['records']),
            completed=sum(item['status'] == 'completed' for item in result['records']),
            evaluation_mode=result['evaluation_mode'], evaluation_batch_id=result['evaluation_batch_id']), ensure_ascii=False))
        return 0
    except (EvaluationError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'error': str(exc) if isinstance(exc, EvaluationError) else type(exc).__name__,
            'action': 'Keep the private manifest; resolve the error and resume the same batch.'}), file=sys.stderr)
        return 1
    finally:
        if platform is not None: platform.close()


if __name__ == '__main__':
    raise SystemExit(main())
