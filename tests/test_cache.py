from haydar.indexer.cache import FileCache


def test_cache_set_get_remove(tmp_haydar):
    cache = FileCache()
    try:
        cache.set("C:/a.txt", 123.0, 10, "hash1", 3)
        row = cache.get("C:/a.txt")
        assert row is not None
        assert row["mtime"] == 123.0
        assert row["size"] == 10
        assert row["file_hash"] == "hash1"
        assert row["chunk_count"] == 3

        assert "C:/a.txt" in cache.get_all_filepaths()

        cache.remove("C:/a.txt")
        assert cache.get("C:/a.txt") is None
    finally:
        cache.close()


def test_cache_remove_many_and_clear(tmp_haydar):
    cache = FileCache()
    try:
        for i in range(5):
            cache.set(f"C:/f{i}.txt", float(i), i, f"h{i}", 1)
        cache.remove_many([f"C:/f{i}.txt" for i in range(3)])
        remaining = cache.get_all_filepaths()
        assert remaining == {"C:/f3.txt", "C:/f4.txt"}

        cache.clear()
        assert cache.get_all_filepaths() == set()
    finally:
        cache.close()


def test_cache_rebuilds_on_corruption(tmp_haydar):
    # Corrupt the DB file, then confirm FileCache recreates it.
    cache = FileCache()
    cache.set("C:/x.txt", 1.0, 1, "h", 1)
    cache.close()

    db_path = cache.db_path
    db_path.write_bytes(b"not a sqlite database")

    cache2 = FileCache()  # _init_db should detect corruption and rebuild
    try:
        # Fresh table, usable again.
        cache2.set("C:/y.txt", 2.0, 2, "h2", 1)
        assert cache2.get("C:/y.txt") is not None
    finally:
        cache2.close()
