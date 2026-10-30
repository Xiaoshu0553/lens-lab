"""Explicit business-data downloads, independent of internal session storage."""
from __future__ import annotations

import csv
from datetime import datetime
import io
import json

from .translation_materials import FORM_ITEMS, SELF_ITEMS
from .translation_quality import RUBRIC


DIMENSIONS = tuple(item['id'] for item in RUBRIC)
RECORD_COLUMNS = (
    'id', 'record_kind', 'condition', 'band',
    'age_band', 'education_stage', 'undergraduate_year', 'translation_experience',
    'ability_score', 'ability_percent', 'translation_literacy_score',
    'translation_knowledge_score', 'translation_judgment_score', 'self_report_mean',
    'created_at', 'ended_at', 'duration_seconds', 'turns',
    'task_title', 'source_text', 'submission', 'answers', 'self_answers', 'feedback',
    'assessment_status', 'score', *DIMENSIONS, 'needs_review', 'review_reason',
    'evaluation_round', 'evaluated_at', 'evaluation_mode', 'evaluation_type',
    'independent_model_count', 'evaluation_session_count',
)
DIALOGUE_COLUMNS = (
    'id', 'record_kind', 'condition', 'band',
    'message_index', 'role', 'content', 'action', 'created_at',
)
RATING_COLUMNS = (
    'id', 'record_kind', 'condition', 'band', 'evaluation_round', 'evaluated_at',
    'evaluation_mode', 'evaluation_type', 'model_id', 'label', 'model',
    'reported_model', 'weight', 'normalized_weight', 'scores',
    'evidence', 'critical_errors', 'total', 'trimmed',
)
COLUMNS = {'records': RECORD_COLUMNS, 'dialogue': DIALOGUE_COLUMNS, 'ratings': RATING_COLUMNS}


def _project(values, columns):
    return {key: values.get(key) for key in columns}


def _common(row):
    return {key: row.get(key) for key in ('id', 'record_kind', 'condition', 'band')}


def _assessment_details(assessment):
    if not assessment:
        return {}
    legacy = any('model_id' not in item for item in assessment.get('ratings', []))
    return dict(
        evaluated_at=assessment.get('evaluated_at'),
        evaluation_mode=assessment.get('evaluation_mode', 'legacy_roles' if legacy else 'distinct_models'),
        evaluation_type=assessment.get('evaluation_type', 'legacy-role-panel' if legacy else 'distinct-model-panel'),
        independent_model_count=assessment.get('independent_model_count'),
        evaluation_session_count=assessment.get('session_count', len(assessment.get('ratings', []))),
    )


def _assessments(row):
    return [*row.get('assessment_history', []), *([row['assessment']] if row.get('assessment') else [])]


def export_record(row, assessment=None):
    """Select research variables; never copy a stored record or audit object."""
    ability = row.get('ability') or {}
    domains = ability.get('domain_scores') or {}
    duration = None
    if row.get('ended_at') and row.get('created_at'):
        duration = round((datetime.fromisoformat(row['ended_at']) -
                          datetime.fromisoformat(row['created_at'])).total_seconds(), 2)
    values = dict(_common(row), **{key: row.get(key) for key in (
        'age_band', 'education_stage', 'undergraduate_year', 'translation_experience',
        'ability_score', 'created_at', 'ended_at', 'submission', 'assessment_status')})
    values.update(
        ability_percent=ability.get('percent'),
        translation_literacy_score=domains.get('翻译素养', {}).get('correct'),
        translation_knowledge_score=domains.get('翻译常识', {}).get('correct'),
        translation_judgment_score=domains.get('译文判断', {}).get('correct'),
        self_report_mean=ability.get('self_report_mean'),
        duration_seconds=duration,
        turns=sum(message.get('role') == 'assistant' for message in row.get('messages', [])),
        task_title=(row.get('task') or {}).get('title'),
        source_text=(row.get('task') or {}).get('source_text'),
        answers={item['id']: row.get('answers', {})[item['id']] for item in FORM_ITEMS
                 if item['id'] in row.get('answers', {})},
        self_answers={item['id']: row.get('self_answers', {})[item['id']] for item in SELF_ITEMS
                      if item['id'] in row.get('self_answers', {})},
        feedback={key: row['feedback'][key] for key in ('burden', 'helpfulness')
                  if key in (row.get('feedback') or {})},
    )
    if assessment:
        values.update(_assessment_details(assessment), score=assessment.get('score'),
            needs_review=assessment.get('needs_review', False), review_reason=assessment.get('reason'),
            **{key: assessment.get('dimensions', {}).get(key) for key in DIMENSIONS})
        values['evaluation_round'] = next((index for index, candidate in enumerate(_assessments(row), 1)
            if candidate == assessment), None)
    return _project(values, RECORD_COLUMNS)


def export_dialogue(row):
    return [_project(dict(_common(row), message_index=index,
        **{key: message.get(key) for key in ('role', 'content', 'action', 'created_at')}),
        DIALOGUE_COLUMNS) for index, message in enumerate(row.get('messages', []), 1)]


def export_ratings(row):
    results = []
    for number, assessment in enumerate(_assessments(row), 1):
        configs = {model['id']: model for model in assessment.get('evaluator_config', [])}
        totals = assessment.get('model_totals', assessment.get('expert_totals', {}))
        trimmed = assessment.get('trimmed_model_ids', assessment.get('trimmed_expert_ids', []))
        for rating in assessment.get('ratings', []):
            ident = rating.get('model_id', rating.get('expert_id'))
            config = configs.get(ident, {})
            values = dict(_common(row), **_assessment_details(assessment),
                evaluation_round=number, model_id=ident,
                label=rating.get('label', config.get('label')),
                model=rating.get('model', config.get('model')),
                reported_model=rating.get('reported_model'),
                weight=rating.get('weight'), normalized_weight=rating.get('normalized_weight'),
                scores={key: rating.get('scores', {}).get(key) for key in DIMENSIONS},
                evidence=rating.get('evidence'), critical_errors=rating.get('critical_errors'),
                total=totals.get(ident, rating.get('total')), trimmed=ident in trimmed)
            results.append(_project(values, RATING_COLUMNS))
    return results


def csv_download(data, kind):
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS[kind], extrasaction='raise')
    writer.writeheader()
    for row in data:
        safe = {}
        for key in COLUMNS[kind]:
            value = row.get(key)
            if isinstance(value, (list, dict)):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
            if isinstance(value, str) and (value.lstrip().startswith(('=', '+', '-', '@'))
                    or value.startswith(('\t', '\r', '\n'))):
                value = "'" + value
            safe[key] = '' if value is None else value
        writer.writerow(safe)
    return '\ufeff' + buffer.getvalue()
