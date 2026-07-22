import pytest

from haydar.config import HaydarConfig
from haydar.search.store import VectorStore, VectorStoreError


def test_store_raises_when_model_missing(tmp_haydar):
    """Regression: VectorStore must raise VectorStoreError (not sys.exit) when
    the embedding model is absent, so callers can handle it gracefully."""
    config = HaydarConfig()
    with pytest.raises(VectorStoreError) as excinfo:
        VectorStore(config, allow_download=False)
    # Friendly hint is attached for the CLI/UI to surface.
    assert excinfo.value.hint
    assert "init" in excinfo.value.hint.lower()
