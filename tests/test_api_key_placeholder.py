"""Public placeholders must never be sent as model credentials."""
from unittest.mock import patch

import pytest

import app.config as config_module


@pytest.mark.parametrize('placeholder', ['自行填写API', '请自行填写API', 'YOUR_API_KEY'])
@pytest.mark.parametrize('mode', ['pilot', 'live'])
def test_remote_model_requires_a_real_configured_key(monkeypatch, placeholder, mode):
    monkeypatch.setattr(config_module, 'load_dotenv', lambda: None)
    with patch.dict('os.environ', {'ADMIN_PASSWORD': 'test-admin', 'STUDY_MODE': mode,
                                  'MODEL_API_KEY': placeholder,
                                  'MODEL_BASE_URL': 'https://example.invalid/v1'}, clear=True):
        with pytest.raises(ValueError, match='请自行填写API'):
            config_module.Settings.from_env()


def test_demo_and_unkeyed_local_model_do_not_send_placeholder(monkeypatch):
    monkeypatch.setattr(config_module, 'load_dotenv', lambda: None)
    for mode in ['demo', 'pilot']:
        with patch.dict('os.environ', {'ADMIN_PASSWORD': 'test-admin', 'STUDY_MODE': mode,
                                      'MODEL_API_KEY': '自行填写API', 'MODEL_NAME': 'test-model',
                                      'MODEL_BASE_URL': 'http://127.0.0.1:11434/v1'}, clear=True):
            assert config_module.Settings.from_env().model_api_key == ''
