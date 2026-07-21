"""
Sentence-transformers wrapper for Haydar embeddings.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

class EmbeddingModel:
    """
    Wrapper for sentence-transformers embedding model with lazy loading.
    """
    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self.model_name = model_name
        self._model = None
        
    def _load(self) -> None:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as e:
                logger.error("sentence-transformers is not installed. Install it with: pip install sentence-transformers")
                raise e
            logger.info(f"Loading embedding model {self.model_name}... (this may take a moment on first run)")
            self._model = SentenceTransformer(self.model_name)
            
    def embed_text(self, text: str) -> list[float]:
        self._load()
        return self._model.encode(text).tolist()
        
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self._load()
        return self._model.encode(texts).tolist()
        
    def get_dimension(self) -> int:
        self._load()
        # Fallback for models without get_sentence_embedding_dimension if needed,
        # but SentenceTransformer models provide this.
        return self._model.get_sentence_embedding_dimension()

_default_model: Optional[EmbeddingModel] = None

def get_model(model_name: str) -> EmbeddingModel:
    """
    Get or create the singleton embedding model.
    """
    global _default_model
    if _default_model is None or _default_model.model_name != model_name:
        _default_model = EmbeddingModel(model_name=model_name)
    return _default_model
