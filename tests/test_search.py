from haydar.search.hybrid import HybridSearch
from haydar.config import HaydarConfig

def test_hybrid_search_empty():
    config = HaydarConfig()
    search = HybridSearch(config)
    results = search.search("")
    assert len(results) == 0
