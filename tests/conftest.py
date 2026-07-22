"""
Shared pytest fixtures for Haydar.

Key concern: ``haydar.config`` computes data-dir paths at import time and other
modules do ``from haydar.config import DB_DIR`` (binding a copy). To isolate
tests from the real ``~/.haydar`` we monkeypatch every module-level copy.
"""

import pytest


@pytest.fixture
def tmp_haydar(tmp_path, monkeypatch):
    """Redirect all Haydar data directories into a temp dir for the test.

    Returns the root temp path. Patches the copies of the path constants that
    each module captured via ``from haydar.config import ...``.
    """
    root = tmp_path / ".haydar"
    db = root / "db"
    logs = root / "logs"
    models = root / "models"
    cache = root / "cache"
    bin_dir = root / "bin"
    for d in (root, db, logs, models, cache, bin_dir):
        d.mkdir(parents=True, exist_ok=True)

    import haydar.config as config

    monkeypatch.setattr(config, "HAYDAR_DIR", root, raising=False)
    monkeypatch.setattr(config, "DB_DIR", db, raising=False)
    monkeypatch.setattr(config, "LOG_DIR", logs, raising=False)
    monkeypatch.setattr(config, "MODELS_DIR", models, raising=False)
    monkeypatch.setattr(config, "CACHE_DIR", cache, raising=False)
    monkeypatch.setattr(config, "RIPGREP_DIR", bin_dir, raising=False)
    monkeypatch.setattr(config, "INDEX_LOCK", root / ".indexing.lock", raising=False)

    # Patch copies captured by other modules if they are already imported.
    for mod_name, names in {
        "haydar.indexer.cache": {"DB_DIR": db},
        "haydar.indexer.extractors": {"CACHE_DIR": cache},
        "haydar.search.store": {"DB_DIR": db, "MODELS_DIR": models},
    }.items():
        try:
            import importlib

            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        for attr, value in names.items():
            monkeypatch.setattr(mod, attr, value, raising=False)

    return root
