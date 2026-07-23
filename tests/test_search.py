from haydar.config import HaydarConfig
from haydar.search.hybrid import HybridSearch


def test_hybrid_search_empty():
    config = HaydarConfig()
    search = HybridSearch(config)
    results = search.search("")
    assert len(results) == 0
