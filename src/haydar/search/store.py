"""
ChromaDB vector store wrapper for Haydar.
"""
from __future__ import annotations

import os
from pathlib import Path

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

from haydar.config import HaydarConfig, DB_DIR


class VectorStore:
    def __init__(self, config: HaydarConfig):
        self.config = config
        self.client = chromadb.PersistentClient(path=str(DB_DIR))
        self.embedding_function = SentenceTransformerEmbeddingFunction(
            model_name=config.embedding_model
        )
        self.collection = self.client.get_or_create_collection(
            name="haydar_files",
            embedding_function=self.embedding_function,
            metadata={"hnsw:space": "cosine"}
        )

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
        try:
            self.client.delete_collection("haydar_files")
        except ValueError:
            pass
        
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
            
        return {m.get("file_path") for m in metadatas if m and "file_path" in m}

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
