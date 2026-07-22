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
        self._store = None

    @property
    def store(self) -> VectorStore:
        """Lazily construct the vector store.

        Deferred so that empty queries and keyword-only searches (which don't
        touch the embedding model) work without a model present, and so any
        VectorStoreError surfaces at query time where callers can handle it.
        """
        if self._store is None:
            self._store = VectorStore(self.config)
        return self._store

    def search_stream(self, query: str, limit: int = 10, mode: str = "semantic", cancel_event=None, worker=None):
        if not query or not query.strip():
            yield []
            return

        if mode == "semantic":
            if cancel_event and cancel_event.is_set():
                return
            try:
                semantic_results = self.store.query(query, n_results=limit * 3)
                if cancel_event and cancel_event.is_set():
                    return
                yield self._merge_and_format([], semantic_results, query, limit)
            except Exception as e:
                logger.error("Semantic search failed: %s", e)
                yield []
        elif mode == "keyword":
            yield from self._stream_ripgrep(query, limit, cancel_event, worker)
            
    def _stream_ripgrep(self, query: str, limit: int, cancel_event, worker):
        import subprocess
        import json
        import time
        import os
        from haydar.config import get_rg_path, HaydarConfigError
        
        try:
            rg_path = get_rg_path()
        except HaydarConfigError as e:
            logger.error(str(e))
            if worker and hasattr(worker, 'error_occurred'):
                worker.error_occurred.emit(str(e))
            return
            
        args = [
            str(rg_path),
            "--json",
            "--ignore-case",
            "--max-columns", "300",
            "--max-count", str(limit * 2),
            query
        ]
        
        for folder in self.config.folders:
            args.append(folder)
            
        try:
            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            
            if worker:
                worker.rg_process = process
                
            skipped = []
            import threading
            def read_stderr():
                if process.stderr:
                    for line in process.stderr:
                        line = line.strip()
                        if line:
                            skipped.append(line)
            
            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stderr_thread.start()
                
            results = []
            last_flush = time.time()
            
            for line in process.stdout:
                if cancel_event and cancel_event.is_set():
                    break
                    
                if not line.strip():
                    continue
                    
                try:
                    data = json.loads(line)
                    if data.get("type") == "match":
                        match = data["data"]
                        file_path = match["path"]["text"]
                        lines = match["lines"]["text"]
                        
                        filename = Path(file_path).name
                        folder = str(Path(file_path).parent)
                        file_type = Path(file_path).suffix
                        
                        try:
                            mod_time = os.path.getmtime(file_path)
                        except:
                            mod_time = 0.0
                            
                        snippet = re.sub(r'\s+', ' ', lines).strip()
                        if len(snippet) > 200:
                            snippet = snippet[:200] + "..."
                            
                        # Avoid duplicates
                        if not any(r.file_path == file_path for r in results):
                            results.append(SearchResult(
                                file_path=file_path,
                                filename=filename,
                                folder=folder,
                                file_type=file_type,
                                snippet=snippet,
                                score=1.0,
                                modified_time=float(mod_time)
                            ))
                except json.JSONDecodeError:
                    pass
                    
                now = time.time()
                if len(results) % 25 == 0 or (now - last_flush) > 0.150:
                    if results:
                        yield list(results)[:limit]
                        last_flush = now
                        
            if results and not (cancel_event and cancel_event.is_set()):
                yield list(results)[:limit]
                
            process.wait()
            stderr_thread.join(timeout=1.0)
            if skipped and worker and hasattr(worker, 'skipped_files'):
                worker.skipped_files.emit(skipped)
                
        except Exception as e:
            logger.error(f"Ripgrep execution failed: {e}")

    def search(self, query: str, limit: int = 10, mode: str = "semantic") -> list[SearchResult]:
        results = []
        for res in self.search_stream(query, limit=limit, mode=mode):
            results = res
        return results

    def _merge_and_format(self, keyword_results, semantic_results, query: str, limit: int) -> list[SearchResult]:
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
                combined[r["id"]]["_score"] = min(1.0, combined[r["id"]]["_score"] * 1.1)
            else:
                r["_score"] = score
                combined[r["id"]] = r

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
            
            start_char = meta.get("start_char")
            end_char = meta.get("end_char")

            snippet = self._extract_snippet(document, query, file_path=file_path, start_char=start_char, end_char=end_char)

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
    def _extract_snippet(document: str, query: str, max_length: int = 200, file_path: str = None, start_char: int = None, end_char: int = None) -> str:
        snippet_text = document
        
        if file_path and start_char is not None and end_char is not None:
            try:
                padding = max_length // 2
                read_start = max(0, start_char - padding)
                read_end = end_char + padding
                
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    f.seek(read_start)
                    context_text = f.read(read_end - read_start)
                
                if context_text.strip():
                    snippet_text = context_text
            except Exception:
                pass
                
        # Clean up whitespace
        snippet_text = re.sub(r'\s+', ' ', snippet_text).strip()
        if not snippet_text:
            return ""

        words = query.split()
        doc_lower = snippet_text.lower()

        for word in sorted(words, key=len, reverse=True):
            idx = doc_lower.find(word.lower())
            if idx != -1:
                start = max(0, idx - (max_length // 2))
                end = min(len(snippet_text), start + max_length)
                
                snippet = snippet_text[start:end].strip()
                if start > 0:
                    snippet = "..." + snippet
                if end < len(snippet_text):
                    snippet = snippet + "..."
                return snippet

        if len(snippet_text) > max_length:
            return snippet_text[:max_length] + "..."
        return snippet_text
