import importlib
import os

def test_jwt_key_from_env(monkeypatch):
    monkeypatch.setenv("TIMETAGGER_JWT_SECRET", "my-test-secret-1234567890")
    from timetagger.server import _utils
    importlib.reload(_utils)
    assert _utils.JWT_KEY == "my-test-secret-1234567890"
