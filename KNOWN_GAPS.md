# Known Gaps

Deliberate omissions, so a future contributor does not re-derive a decision that
was already made and recorded. Each entry states what is missing, why, and what
would have to change for it to be revisited.

---

## Automated native OCR (Tesseract) provisioning

**Status:** deliberately not implemented. One-click OCR installation fails
closed with `asset_not_reviewed`.

Haydar ships the full provisioning transaction — pinned manifest, streamed
download, constant-time SHA-256 comparison, member-by-member extraction,
executable probe, atomic activation (`src/haydar/ocr.py`). What it does *not*
ship is a pinned asset for it to install, because a licensing and integrity
review on 2026-08-10 found no Windows Tesseract distribution that satisfies the
product contract's distribution rule ("do not bundle an unlicensed/unreviewed
native OCR archive"):

- Upstream publishes exactly one Windows artifact per release, an NSIS `.exe`
  installer. It bundles pango (LGPL-2.1, pulling glib2 and cairo) while shipping
  only Tesseract's own notices, and it fetches language data from a branch URL
  that cannot be pinned. It is also an installer, not an archive, so it cannot be
  extracted and activated programmatically.
- conda-forge's package is licence-clean and hash-published, but ships only
  `tesseract.exe` plus one DLL; leptonica, libarchive, libcurl, libtiff, and the
  VC runtime are separate packages. Supporting it needs a multi-artifact manifest
  and a zstd reader — a design change, not a pin.
- No authoritative portable zip exists. The "portable zip" repositories on GitHub
  are third-party repacks of old versions.

Neither path is worth its cost for an optional feature, so the manifest stays
`PENDING_REVIEW` and provisioning refuses to run rather than downloading
something unverified.

**What still works.** A Tesseract the user installed themselves is detected and
used: `_find_system_tesseract()` checks `PATH` and the conventional Windows
install directories, and `install_ocr()` returns ready when it finds a supported
one without modifying it. Images encountered while OCR is unavailable are
recorded as `ocr_deferred`, never as processed, so they are picked up by an
image-only backfill if an engine appears later — no reindex.

**This is now documented to users (rule amended 2026-08-11).** The docs rule
originally forbade directing users to a manual OCR installation, on the
assumption that one-click provisioning would ship. Since it does not, that ban
left "not available in this build" as the whole story while a working route
existed. The docs, `get_install_instructions()`, and the Settings dialog now name
Tesseract, state the v4+/English-language-data requirements, and say to restart
Haydar. Package managers, third-party mirrors, and PATH edits remain forbidden,
with wider assertions in `test_docs_contract.py`.

The amendment is narrow. Permitted: naming Tesseract, stating its v4+ and
English-language-data requirements, and linking the Tesseract project's own
installer. Still forbidden: package-manager invocations (`winget`, `choco`,
`scoop`), third-party mirrors such as UB-Mannheim, PATH edits, `pip install`, and
making `haydar-cli.exe` the normal path. Detection reads `PATH` and the default
install directories, so the copy stops at "install it, restart Haydar" and never
asks a user to wire anything up. Reverting the amendment is a copy change plus
the assertions in `test_ocr.py`, `test_settings.py`, `test_ocr_provisioning.py`,
and `test_docs_contract.py`.

**Constraint on any revisit.** The amendment is scoped to the unreviewed state.
If an asset is ever pinned, the one-click copy must stay engine-agnostic —
`test_install_instructions_never_direct_a_user_to_pip_winget_or_path` asserts
that naming Tesseract does not leak into it.

**To revisit**, in order: choose a distribution, settle its redistribution terms,
record the outcome in `THIRD_PARTY_NOTICES.md`, then set `url`/`sha256` in
`OCR_ASSETS` and update `test_the_shipped_manifest_entry_is_marked_unreviewed`.
`ENG_TRAINEDDATA_URL` / `ENG_TRAINEDDATA_SHA256` in `src/haydar/ocr.py` are
already reviewed and pinned (Apache-2.0, commit-pinned, downloaded and hashed
during the review), so the language-data half needs no further work.

Full findings: the review comment above `OCR_ASSETS` in `src/haydar/ocr.py` and
the Tesseract section of `THIRD_PARTY_NOTICES.md`.

---

## Unsigned executables

**Status:** accepted for now.

Release EXEs are not code-signed, so Windows SmartScreen warns on first launch.
A certificate is a recurring cost; the mitigation is published SHA-256 checksums
plus `verify.ps1`, documented in `README.md` and `docs/installation.md`.

---

## Packaged-GUI startup is proven only in CI

**Status:** by design, but worth knowing.

`scripts/packaged_startup_probe.py` runs the frozen `haydar.exe` offscreen in a
throwaway profile and gates the release on it. It cannot run in the normal test
suite, because it needs a PyInstaller artifact only the build job produces —
`tests/test_packaging.py` covers the same report and rules against the
unpackaged entry point instead. A packaging regression is therefore caught at
release time, not on a pull request.
