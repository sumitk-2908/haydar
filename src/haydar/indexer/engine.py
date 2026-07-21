"""
Main indexing orchestrator for Haydar.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

from haydar.config import HaydarConfig, ALL_INDEXABLE_EXTENSIONS, is_excluded, get_size_category
from haydar.indexer.extractors import extract_text
from haydar.search.store import VectorStore

logger = logging.getLogger(__name__)

class IndexingEngine:
    def __init__(self, config: HaydarConfig):
        self.config = config
        self.store = VectorStore(config)

    def index_all(self, force: bool = False) -> dict:
        """Index all files in configured folders."""
        stats = {
            "files_indexed": 0,
            "chunks_stored": 0,
            "files_skipped_size": 0,
            "files_skipped_error": 0,
            "files_skipped_unchanged": 0,
            "total_text_bytes": 0
        }

        files_to_process = []
        for folder_path in self.config.folders:
            folder = Path(folder_path)
            if not folder.exists() or not folder.is_dir():
                continue
                
            for filepath in folder.rglob('*'):
                if not filepath.is_file():
                    continue
                files_to_process.append(filepath)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("({task.completed}/{task.total})"),
        ) as progress:
            task = progress.add_task("[cyan]Indexing files...", total=len(files_to_process))
            
            for filepath in files_to_process:
                progress.advance(task)
                
                ext = filepath.suffix.lower()
                if ext not in ALL_INDEXABLE_EXTENSIONS:
                    continue
                    
                if is_excluded(filepath, self.config.excluded_patterns):
                    continue
                    
                try:
                    file_size = filepath.stat().st_size
                except OSError:
                    stats["files_skipped_error"] += 1
                    continue
                    
                category = get_size_category(ext)
                limit = self.config.size_limits.get(category, 0)
                if file_size > limit:
                    size_mb = file_size / (1024 * 1024)
                    limit_mb = limit / (1024 * 1024)
                    logger.warning(f"Skipped {filepath.name} ({size_mb:.1f} MB) — exceeds {category} limit of {limit_mb:.1f} MB")
                    stats["files_skipped_size"] += 1
                    continue

                try:
                    file_hash = self._compute_hash(filepath)
                except OSError:
                    stats["files_skipped_error"] += 1
                    continue
                    
                if not force:
                    existing_hash = self.store.get_file_hash(str(filepath.absolute()))
                    if existing_hash == file_hash:
                        stats["files_skipped_unchanged"] += 1
                        continue

                try:
                    extracted = extract_text(filepath)
                    if not extracted or not extracted.text.strip():
                        stats["files_skipped_error"] += 1
                        continue
                        
                    stats["total_text_bytes"] += len(extracted.text.encode('utf-8'))
                    chunks = self._chunk_text(extracted.text, self.config.chunk_size, self.config.chunk_overlap)
                    
                    if not chunks:
                        stats["files_skipped_error"] += 1
                        continue
                        
                    self.store.delete_by_filepath(str(filepath.absolute()))
                    
                    ids = []
                    documents = []
                    metadatas = []
                    
                    mod_time = os.path.getmtime(filepath)
                    
                    for i, chunk in enumerate(chunks):
                        ids.append(f"{file_hash}_{i}")
                        documents.append(chunk)
                        metadatas.append({
                            "file_path": str(filepath.absolute()),
                            "file_type": ext,
                            "chunk_index": i,
                            "file_hash": file_hash,
                            "modified_time": mod_time,
                            "filename": filepath.name
                        })
                        
                    batch_size = 100
                    for i in range(0, len(ids), batch_size):
                        self.store.add_documents(
                            ids=ids[i:i + batch_size],
                            documents=documents[i:i + batch_size],
                            metadatas=metadatas[i:i + batch_size]
                        )
                        stats["chunks_stored"] += len(ids[i:i + batch_size])
                        
                    stats["files_indexed"] += 1
                    
                except Exception as e:
                    logger.error(f"Error indexing {filepath}: {e}")
                    stats["files_skipped_error"] += 1

        return stats

    def index_file(self, filepath: Path) -> bool:
        """Index or re-index a single file."""
        if not filepath.exists() or not filepath.is_file():
            return False
            
        ext = filepath.suffix.lower()
        if ext not in ALL_INDEXABLE_EXTENSIONS:
            return False
            
        try:
            file_hash = self._compute_hash(filepath)
            extracted = extract_text(filepath)
            if not extracted or not extracted.text.strip():
                return False
                
            chunks = self._chunk_text(extracted.text, self.config.chunk_size, self.config.chunk_overlap)
            if not chunks:
                return False
                
            self.store.delete_by_filepath(str(filepath.absolute()))
            
            ids = []
            documents = []
            metadatas = []
            mod_time = os.path.getmtime(filepath)
            
            for i, chunk in enumerate(chunks):
                ids.append(f"{file_hash}_{i}")
                documents.append(chunk)
                metadatas.append({
                    "file_path": str(filepath.absolute()),
                    "file_type": ext,
                    "chunk_index": i,
                    "file_hash": file_hash,
                    "modified_time": mod_time,
                    "filename": filepath.name
                })
                
            batch_size = 100
            for i in range(0, len(ids), batch_size):
                self.store.add_documents(
                    ids=ids[i:i + batch_size],
                    documents=documents[i:i + batch_size],
                    metadatas=metadatas[i:i + batch_size]
                )
                
            return True
        except Exception:
            return False

    def remove_file(self, filepath: Path) -> None:
        """Remove a file from the index."""
        self.store.delete_by_filepath(str(filepath.absolute()))

    @staticmethod
    def _chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
        """Split text into overlapping chunks of words."""
        words = text.split()
        chunks = []
        
        if not words:
            return chunks
            
        i = 0
        while i < len(words):
            end_idx = min(i + chunk_size, len(words))
            chunk_words = words[i:end_idx]
            
            if len(chunk_words) < 20 and chunks:
                break
                
            chunks.append(" ".join(chunk_words))
            
            if end_idx == len(words):
                break
                
            i += (chunk_size - overlap)
            
        return chunks

    @staticmethod
    def _compute_hash(filepath: Path) -> str:
        """Compute MD5 hash of first 8KB concatenated with file size."""
        file_size = filepath.stat().st_size
        hasher = hashlib.md5()
        
        with open(filepath, 'rb') as f:
            chunk = f.read(8192)
            hasher.update(chunk)
            
        hasher.update(str(file_size).encode('utf-8'))
        return hasher.hexdigest()
