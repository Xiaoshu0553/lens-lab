"""Isolated model-panel fixtures; these are test doubles, never live API evidence."""
from __future__ import annotations

from test_flow import auth


def panel_models():
    return [dict(id=f'judge{i}', label=f'AI {i}', model=f'distinct-model-{i}',
                 base_url=f'https://judge{i}.example.invalid/v1', api_key=f'test-secret-{i}',
                 weight=1, max_tokens=4096, json_mode=True)
            for i in range(1, 6)]


def configure_panel(app, client, provider=None, providers=None):
    models = panel_models()
    response = client.put('/api/admin/translation/models', json={'judges': models}, headers=auth())
    assert response.status_code == 200, response.text
    if providers is not None:
        app.state.translation_judge_providers = providers
    elif provider is not None:
        app.state.translation_judge_providers = {item['id']: provider for item in models}
    return models
