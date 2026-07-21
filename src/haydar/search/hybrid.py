import re
import logging
from dataclasses import dataclass
from pathlib import Path

from haydar.config import HaydarConfig
from haydar.search.store import VectorStore

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    file_path: str
    filename: str
    folder: str
    file_type: str
    snippet: str
    score: float
    modified_time: float


class HybridSearch:
    def __init__(self, config: HaydarConfig):
        self.config = config
        self.store = VectorStore(config)

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        if not query or not query.strip():
            return []

        # 1. Semantic Search
        try:
            semantic_results = self.store.query(query, n_results=limit * 3)
        except Exception as e:
            logger.error("Semantic search failed: %s", e)
            semantic_results = []

        # 2. Keyword Search
        keyword_results = []
        words = query.split()
        if len(words) > 1:
            longest_word = max(words, key=len)
            try:
                keyword_results = self.store.query_with_filter(
                    query, 
                    where_document={"$contains": longest_word}, 
                    n_results=limit * 2
                )
            except Exception as e:
                logger.error("Keyword search failed: %s", e)
                keyword_results = []

        # Combine and score
        combined = {}

        for r in semantic_results:
            if not r.get("id"):
                continue
            distance = r.get("distance")
            if distance is None:
                distance = 0.0
            score = max(0.0, min(1.0, 1.0 - distance))
            r["_score"] = score
            combined[r["id"]] = r

        for r in keyword_results:
            if not r.get("id"):
                continue
            distance = r.get("distance")
            if distance is None:
                distance = 0.0
            score = max(0.0, min(1.0, 1.0 - distance))
            
            if r["id"] in combined:
                # Boost if in both
                combined[r["id"]]["_score"] = min(1.0, combined[r["id"]]["_score"] * 1.1)
            else:
                r["_score"] = score
                combined[r["id"]] = r

        # Deduplicate by file_path
        best_per_file = {}
        for r in combined.values():
            meta = r.get("metadata") or {}
            file_path = meta.get("file_path")
            if not file_path:
                continue

            current_score = r["_score"]
            if file_path not in best_per_file or current_score > best_per_file[file_path]["_score"]:
                best_per_file[file_path] = r

        sorted_results = sorted(best_per_file.values(), key=lambda x: x["_score"], reverse=True)
        top_results = sorted_results[:limit]

        final_results = []
        for r in top_results:
            meta = r.get("metadata") or {}
            file_path = meta.get("file_path", "")
            filename = meta.get("filename", Path(file_path).name)
            file_type = meta.get("file_type", Path(file_path).suffix)
            modified_time = meta.get("modified_time", 0.0)
            document = r.get("document", "")

            snippet = self._extract_snippet(document, query)

            final_results.append(SearchResult(
                file_path=file_path,
                filename=filename,
                folder=str(Path(file_path).parent),
                file_type=file_type,
                snippet=snippet,
                score=r["_score"],
                modified_time=float(modified_time)
            ))

        return final_results

    @staticmethod
    def _extract_snippet(document: str, query: str, max_length: int = 200) -> str:
        # Clean up whitespace
        document = re.sub(r'\s+', ' ', document).strip()
        if not document:
            return ""

        words = query.split()
        doc_lower = document.lower()

        # Try to find the query terms in the document
        for word in sorted(words, key=len, reverse=True):
            idx = doc_lower.find(word.lower())
            if idx != -1:
                start = max(0, idx - (max_length // 2))
                end = min(len(document), start + max_length)
                
                snippet = document[start:end].strip()
                if start > 0:
                    snippet = "..." + snippet
                if end < len(document):
                    snippet = snippet + "..."
                return snippet

        # If not found, return the first max_length characters
        if len(document) > max_length:
            return document[:max_length] + "..."
        return document
