"""
Shared pytest fixtures for Haydar.

Key concern: ``haydar.config`` computes data-dir paths at import time and other
modules do ``from haydar.config import DB_DIR`` (binding a copy). To isolate
tests from the real ``~/.haydar`` we monkeypatch every module-level copy.
"""

import shutil

import pytest


@pytest.fixture(autouse=True)
def _never_delete_the_real_profile(monkeypatch):
    """Fail any test that recursively deletes the developer's real ``~/.haydar``.

    Import-time path binding makes this a live hazard rather than a theoretical
    one: a test that exercises a destructive command reaches the *module's* copy
    of ``HAYDAR_DIR``, which ``tmp_haydar`` does not patch unless that module is
    listed. A test that forgets one deletes real data, and the failure looks like
    an ordinary assertion error. This turns it into a loud, harmless failure
    instead.
    """
    real_root = __import__("pathlib").Path.home() / ".haydar"
    real_rmtree = shutil.rmtree

    def guarded_rmtree(path, *args, **kwargs):
        resolved = __import__("pathlib").Path(str(path)).expanduser()
        if resolved == real_root or real_root in resolved.parents:
            raise AssertionError(
                f"a test tried to delete the real Haydar profile at {resolved}; "
                "patch the calling module's HAYDAR_DIR (see tmp_haydar) or stub "
                "shutil.rmtree"
            )
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", guarded_rmtree)


@pytest.fixture
def tmp_haydar(tmp_path, monkeypatch):
    """Redirect all Haydar data directories into a temp dir for the test.

    Returns the root temp path. Patches the copies of the path constants that
    each module captured via ``from haydar.config import ...``.
    """
    root = tmp_path / "haydar-data"
    db = root / "db"
    logs = root / "logs"
    models = root / "models"
    cache = root / "cache"
    bin_dir = root / "bin"
    ocr = root / "ocr"
    ocr_versions = ocr / "versions"
    ocr_staging = ocr / "staging"
    for d in (root, db, logs, models, cache, bin_dir, ocr, ocr_versions, ocr_staging):
        d.mkdir(parents=True, exist_ok=True)

    import haydar.config as config

    monkeypatch.setattr(config, "HAYDAR_DIR", root, raising=False)
    monkeypatch.setattr(config, "DB_DIR", db, raising=False)
    monkeypatch.setattr(config, "LOG_DIR", logs, raising=False)
    monkeypatch.setattr(config, "MODELS_DIR", models, raising=False)
    monkeypatch.setattr(config, "CACHE_DIR", cache, raising=False)
    monkeypatch.setattr(config, "RIPGREP_DIR", bin_dir, raising=False)
    monkeypatch.setattr(config, "INDEX_LOCK", root / ".indexing.lock", raising=False)
    monkeypatch.setattr(config, "CONFIG_PATH", root / "config.json", raising=False)
    monkeypatch.setattr(config, "OCR_DIR", ocr, raising=False)
    monkeypatch.setattr(config, "OCR_VERSIONS_DIR", ocr_versions, raising=False)
    monkeypatch.setattr(config, "OCR_STAGING_DIR", ocr_staging, raising=False)
    monkeypatch.setattr(
        config, "OCR_CURRENT_POINTER", ocr / "current.json", raising=False
    )

    # Patch copies captured by other modules if they are already imported.
    for mod_name, names in {
        "haydar.indexer.cache": {"DB_DIR": db},
        "haydar.indexer.extractors": {"CACHE_DIR": cache},
        "haydar.search.store": {"DB_DIR": db, "MODELS_DIR": models},
        "haydar.setup": {"RIPGREP_DIR": bin_dir},
        # cli binds HAYDAR_DIR at import; `uninstall` deletes what it points at.
        "haydar.cli": {"HAYDAR_DIR": root, "DB_DIR": db},
        "haydar.logging_setup": {"LOG_DIR": logs},
        "haydar.ocr": {

            "OCR_DIR": ocr,
            "OCR_VERSIONS_DIR": ocr_versions,
            "OCR_STAGING_DIR": ocr_staging,
            "OCR_CURRENT_POINTER": ocr / "current.json",
        },
    }.items():
        try:
            import importlib

            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        for attr, value in names.items():
            monkeypatch.setattr(mod, attr, value, raising=False)

    return root
