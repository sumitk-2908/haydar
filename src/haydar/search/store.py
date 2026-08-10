"""
ChromaDB vector store wrapper for Haydar.
"""
from __future__ import annotations

import os
from contextlib import suppress

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

from haydar.config import DB_DIR, MODELS_DIR, HaydarConfig


def _model_cache_name(model_name: str) -> str:
    """Return Hugging Face's cache directory name for a configured model."""
    normalized = model_name.strip().replace("\\", "/").strip("/")
    if "/" not in normalized:
        normalized = f"sentence-transformers/{normalized}"
    return "models--" + normalized.replace("/", "--")


def _classify_storage_error(exc: Exception, operation: str) -> tuple[str, str]:
    """Map storage failures to bounded, safe user messages and remedies."""
    detail = str(exc).lower()
    if isinstance(exc, PermissionError) or "permission" in detail or "access is denied" in detail:
        return (
            f"The vector database at {DB_DIR} cannot be accessed.",
            "Check folder permissions and close other Haydar processes, then try again.",
        )
    if "lock" in detail or "in use" in detail or "busy" in detail:
        return (
            f"The vector database at {DB_DIR} is currently in use.",
            "Close other Haydar processes and try again.",
        )
    if "corrupt" in detail or "malformed" in detail or "not a database" in detail:
        return (
            f"The vector database at {DB_DIR} is corrupt.",
            "Run `haydar-cli.exe reindex` to recreate the database; your files are unaffected.",
        )
    return (
        f"The vector database {operation} failed.",
        "Check available disk space and the full log, then try again.",
    )


class VectorStoreError(Exception):
    """A bounded user-facing vector-store failure with an actionable hint."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


class VectorStore:
    def __init__(
        self, config: HaydarConfig, allow_download: bool = True
    ) -> None:
        self.config = config

        # Ensure sentence-transformers caches models in our directory. Done here
        # (not at import time) so importing this module has no global side effects.
        os.environ["SENTENCE_TRANSFORMERS_HOME"] = str(MODELS_DIR)

        # A cache for some unrelated model is insufficient. Require this exact
        # configured model and at least one snapshot payload.
        expected_model = MODELS_DIR / _model_cache_name(config.embedding_model)
        model_complete = expected_model.is_dir() and any(
            path.is_file() for path in expected_model.glob("snapshots/**/*")
        )
        if not allow_download and not model_complete:
            raise VectorStoreError(
                f"The embedding model '{config.embedding_model}' was not found locally.",
                hint="Run `haydar-cli.exe init` to download the configured model.",
            )

        try:
            self.client = chromadb.PersistentClient(path=str(DB_DIR))
        except Exception as exc:
            message, hint = _classify_storage_error(exc, "could not be opened")
            raise VectorStoreError(message, hint=hint) from exc

        try:
            self.embedding_function = SentenceTransformerEmbeddingFunction(
                model_name=config.embedding_model
            )
        except Exception as exc:
            hint = (
                "Please check your internet connection and try again."
                if allow_download
                else "Run `haydar-cli.exe init` to download the embedding model."
            )
            raise VectorStoreError(
                f"The embedding model '{config.embedding_model}' could not be loaded.",
                hint=hint,
            ) from exc

        try:
            self.collection = self.client.get_or_create_collection(
                name="haydar_files",
                embedding_function=self.embedding_function,
                metadata={"hnsw:space": "cosine"}
            )
        except Exception as exc:
            message, hint = _classify_storage_error(exc, "collection initialization")
            raise VectorStoreError(message, hint=hint) from exc

    def embed_probe(self, text: str = "haydar embedding probe") -> int:
        """Prove the configured model can actually embed text; return its dimension.

        Constructing the embedding function is not sufficient evidence that the
        model is usable — a partially downloaded snapshot can import cleanly and
        then fail on first use. Setup calls this so that failure surfaces during
        provisioning rather than on the user's first search.
        """
        vectors = self.embedding_function([text])
        if not vectors or len(vectors) != 1 or len(vectors[0]) == 0:
            raise VectorStoreError(
                f"The embedding model '{self.config.embedding_model}' returned no "
                "usable output.",
                hint="Try setup again to re-download the model.",
            )
        return len(vectors[0])

    def verify_readable(self) -> None:
        """Run bounded count and query capability checks against the collection.

        This never writes and never clears: an existing user's index must be
        readable after an upgrade without being rebuilt.
        """
        self.collection.count()
        # A query against an empty collection is valid and returns no ids, which
        # is exactly the check we want: it exercises the read path end to end
        # without depending on any indexed content.
        self.collection.query(query_texts=["haydar readiness probe"], n_results=1)

    def add_documents(self, ids: list[str], documents: list[str], metadatas: list[dict]) -> None:
        """Add documents to the collection."""
        if not ids:
            return
        self.collection.upsert(
            ids=ids,
            documents=documents,
            metadatas=metadatas
        )

    def delete_by_filepath(self, filepath: str) -> None:
        """Delete all chunks belonging to a specific file path."""
        results = self.collection.get(
            where={"file_path": filepath},
            include=[]
        )
        if results and results.get("ids"):
            self.collection.delete(ids=results["ids"])

    def delete_by_filepaths(self, filepaths: list[str]) -> None:
        """Delete all chunks belonging to a list of file paths."""
        if not filepaths:
            return

        # ChromaDB 'in' operator has limits, but we can do it via get+delete
        # To handle potentially large lists, we fetch and delete in batches of 100
        batch_size = 100
        for i in range(0, len(filepaths), batch_size):
            batch = filepaths[i:i + batch_size]
            results = self.collection.get(
                where={"file_path": {"$in": batch}},
                include=[]
            )
            if results and results.get("ids"):
                self.collection.delete(ids=results["ids"])

    def query(self, query_text: str, n_results: int = 10) -> list[dict]:
        """Semantic search."""
        results = self.collection.query(
            query_texts=[query_text],
            n_results=n_results,
            include=["documents", "metadatas", "distances"]
        )
        return self._format_query_results(results)

    def query_with_filter(self, query_text: str, where_document: dict | None = None, n_results: int = 10) -> list[dict]:
        """Search with optional document content filter."""
        results = self.collection.query(
            query_texts=[query_text],
            n_results=n_results,
            where_document=where_document,
            include=["documents", "metadatas", "distances"]
        )
        return self._format_query_results(results)

    def _format_query_results(self, results: dict) -> list[dict]:
        """Format ChromaDB query results into a list of dicts."""
        formatted_results = []
        if not results.get("ids") or not results["ids"]:
            return formatted_results

        ids = results["ids"][0]
        documents = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        for i in range(len(ids)):
            formatted_results.append({
                "id": ids[i],
                "document": documents[i] if documents and i < len(documents) else None,
                "metadata": metadatas[i] if metadatas and i < len(metadatas) else None,
                "distance": distances[i] if distances and i < len(distances) else None
            })

        return formatted_results

    def get_stats(self) -> dict:
        """Return stats: files_indexed, chunks_stored, db_size_bytes."""
        chunks_stored = self.collection.count()

        all_metadatas = self.collection.get(include=["metadatas"])["metadatas"]
        if all_metadatas:
            files_indexed = len(set(m.get("file_path") for m in all_metadatas if m and "file_path" in m))
        else:
            files_indexed = 0

        db_size_bytes = 0
        if DB_DIR.exists():
            for root, _, files in os.walk(str(DB_DIR)):
                for file in files:
                    file_path = os.path.join(root, file)
                    if not os.path.islink(file_path):
                        db_size_bytes += os.path.getsize(file_path)

        return {
            "files_indexed": files_indexed,
            "chunks_stored": chunks_stored,
            "db_size_bytes": db_size_bytes
        }

    def clear(self) -> None:
        """Delete the entire collection and recreate it."""
        with suppress(ValueError):
            self.client.delete_collection("haydar_files")

        self.collection = self.client.get_or_create_collection(
            name="haydar_files",
            embedding_function=self.embedding_function,
            metadata={"hnsw:space": "cosine"}
        )

    def get_all_file_paths(self) -> set[str]:
        """Return all unique file paths in the index."""
        results = self.collection.get(include=["metadatas"])
        metadatas = results.get("metadatas", [])
        if not metadatas:
            return set()

        return {str(m["file_path"]) for m in metadatas if m and "file_path" in m}

    def get_file_hash(self, filepath: str) -> str | None:
        """Get stored hash for a file path."""
        results = self.collection.get(
            where={"file_path": filepath},
            include=["metadatas"],
            limit=1
        )
        metadatas = results.get("metadatas")
        if metadatas and metadatas[0] and "file_hash" in metadatas[0]:
            return str(metadatas[0]["file_hash"])
        return None
