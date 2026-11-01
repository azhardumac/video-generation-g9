import os

import pytest

from app.gemini_secrets import GeminiSecretStore, SecretStoreError


def test_file_secret_is_owner_only_and_never_returned_as_metadata(tmp_path):
    path = tmp_path / "private" / "gemini.json"
    store = GeminiSecretStore(path, env={})
    assert store.save("a-realistic-but-fake-test-key") == "local_file"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    value, source = store.get()
    assert value == "a-realistic-but-fake-test-key"
    assert source == "local_file"
    assert store.delete() == "local_file"
    assert store.get() == (None, None)


def test_environment_key_overrides_file_and_cannot_be_replaced_or_deleted(tmp_path):
    path = tmp_path / "gemini.json"
    store = GeminiSecretStore(path, env={"GEMINI_API_KEY": "environment-secret"})
    assert store.get() == ("environment-secret", "environment")
    with pytest.raises(SecretStoreError, match="backend environment"):
        store.save("replacement")
    with pytest.raises(SecretStoreError, match="backend environment"):
        store.delete()
    assert not path.exists()


def test_rejects_broad_permissions_symlinks_and_malformed_keys(tmp_path):
    path = tmp_path / "secret.json"
    path.write_text('{"gemini_api_key":"secret"}')
    path.chmod(0o644)
    with pytest.raises(SecretStoreError, match="permissions"):
        GeminiSecretStore(path, env={}).get()
    path.chmod(0o600)
    linked = tmp_path / "link.json"
    linked.symlink_to(path)
    with pytest.raises(SecretStoreError, match="symbolic link"):
        GeminiSecretStore(linked, env={}).get()
    with pytest.raises(ValueError, match="valid Gemini API key"):
        GeminiSecretStore(tmp_path / "new.json", env={}).save("bad\nkey")
