# Haydar Agent Guidelines

## Architecture Boundary
Haydar maintains a strict two-layer architecture:
- `src/haydar/ui/`: Contains all PySide6 frontend UI components.
- `src/haydar/indexer/` and `src/haydar/search/`: Contains all backend logic.

**CRITICAL RULE**: UI modules in `src/haydar/ui/` may only import from `src/haydar/search/` (the interface layer) or `src/haydar/config.py`. UI modules must NEVER import from `src/haydar/indexer/` internals directly. This ensures the backend can be decoupled or refactored without breaking the UI.
