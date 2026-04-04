# Nodriver vs Zendriver technical divergence report (as of 2026-04-04)

## Scope and source baseline

- **Nodriver baseline:** local working tree (`nodriver` package version `0.48.1`).
- **Zendriver baseline:** PyPI source distribution `zendriver==0.15.3` (released **2026-03-11**).
- This report compares shipped source/layout and public API surface in `core/` and packaging metadata.

## Executive summary

Zendriver is no longer just a rebrand/fork rename. The fork has diverged in three important ways:

1. **API/feature expansion around network/event workflows** (request/response expectations, fetch interception, download expectations, PDF/MHTML/base64 helpers).
2. **Architecture cleanup in internals** (modularized helper code, explicit connection target properties, stronger typing and testing/tooling posture).
3. **Operational hardening** (browser startup tuning, Brave support, anti-detection flags like WebRTC controls, and several race-condition/edge-case fixes documented in their changelog).

If you are deciding what to backport, the highest-value candidates are likely the expectation/interception APIs, keyboard/input rewrite, and selected bug fixes called out below.

## 1) Dependencies and packaging changes

### Runtime dependencies

Compared to nodriver's current runtime dependencies (`mss`, `websockets>=14`, `deprecated`), zendriver adds:

- `asyncio-atexit>=1.0.1`
- `emoji>=2.14.1`
- `grapheme>=0.6.0`
- tighter minimum pins for existing deps (`deprecated`, `mss`, `websockets`).

**Interpretation:**
- `emoji` + `grapheme` directly support richer Unicode key/input processing (see `core/keys.py`).
- `asyncio-atexit` suggests stronger lifecycle/cleanup handling.

### Build/dev tooling

- Nodriver uses `setuptools` build backend and Sphinx-oriented optional deps.
- Zendriver moved to `hatchling`, `uv` dev dependencies, `ruff`, `mypy` (strict), `pytest` + coverage, and MkDocs.

**Implication:** Zendriver has a more opinionated static-analysis/testing pipeline, which generally improves maintainability and regression prevention.

## 2) Module structure and refactoring

### New/removed modules

- **Added in zendriver core:**
  - `core/expect.py`
  - `core/intercept.py`
  - `core/cloudflare.py`
  - `core/keys.py`
  - package-level `core/__init__.py`
- **Only in nodriver CDP tree:** `cdp/database.py` (not present in zendriver 0.15.3).

### Refactor direction

Zendriver appears to have split behavior that was previously tab/util-centric into focused modules:

- **network expectations** into `expect.py`
- **Fetch domain interception** into `intercept.py`
- **Cloudflare challenge utilities** into `cloudflare.py`
- **keyboard model/event synthesis** into `keys.py`

This is a classic maintainability refactor: narrower modules, clearer responsibilities, easier independent testing.

## 3) Public API divergences (core classes)

### Tab API (high-signal changes)

Methods **added in zendriver** (vs nodriver):

- `expect_request`, `expect_response`, `expect_download`
- `intercept`
- `save_snapshot`, `print_to_pdf`, `screenshot_b64`
- `set_user_agent`, `wait_for_ready_state`, `disable_dom_agent`

Methods present in nodriver but **not present in zendriver** include:

- `wait`, `sleep`, `feed_cdp`
- frame-tree utilities (`get_frame_tree`, `get_frame_resource_tree`, `search_frame_resources`, etc.)
- `template_location`, `bypass_insecure_connection_warning`, `mouse_drag`, `scroll_bottom_reached`

**Interpretation:** Zendriver has prioritized deterministic network/document workflow APIs and convenience output methods over some of nodriver’s legacy utility helpers.

### Element API

Zendriver adds:

- `clear_input_by_deleting`
- `get(name: str)` accessor
- `screenshot_b64`

and removes/deprecates patterns that relied on dynamic attribute access in favor of explicit APIs (also reflected in changelog notes).

### Connection API / browser interaction layer

Zendriver introduces richer target metadata properties and lifecycle helpers on `Connection` (`target_id`, `type_`, `title`, `url`, `attached`, `opener_*`, `browser_context_id`, etc.), plus `aopen/aclose`, `remove_handlers`, and listener controls.

This aligns with changelog entries describing refactors to avoid blocking listener loops and race-condition fixes in tab/browser target handling.

## 4) Browser startup/configuration differences

Zendriver `Config` expands launch control beyond nodriver with:

- explicit `browser` selection (`chrome` / `brave` / `auto`)
- connection retry tuning (`browser_connection_timeout`, `browser_connection_max_tries`)
- `user_agent`
- `disable_webrtc` and `disable_webgl`
- lazy profile directory creation semantics

These are practical anti-detection/stability controls and align with their changelog (Brave support, WebRTC leak fixes, headless/cloudflare bypass-related adjustments).

## 5) Bug fixes & performance/robustness work worth reviewing for backport

From zendriver changelog through `0.15.3` (selected high-relevance items):

- Race-condition fixes in selector/query and browser/tab target lifecycle.
- Async handler/listener refactor to avoid blocking event loop processing.
- Keyboard/input rewrite with better modifiers, special keys, and mixed input handling.
- `evaluate()` serialization fixes for falsy/JSON return correctness.
- Fixes around React-controlled input clearing (`_valueTracker`) and delete-key edge cases.
- Cookie parsing compatibility fix for Chrome 146 (`sameParty` field missing scenario).
- Cross-platform compatibility improvements, especially Windows process/subprocess handling.

These are likely the most backportable changes with immediate user value.

## 6) Browser interaction / protocol handling observations

- Zendriver significantly extends **network protocol ergonomics** with context-manager-based request/response expectations and fetch interception wrappers.
- CDP bindings have diverged broadly (large generated-file diffs), indicating schema refresh cadence in zendriver.
- Removal of certain dynamic magic (`__getattr__` behaviors per changelog) in favor of explicit properties/methods suggests a deliberate move toward safer typed interfaces.

## 7) Recommendation for nodriver maintainers

If your goal is selective convergence without absorbing the entire fork, prioritize in this order:

1. **Reliability patches** (race conditions, listener loop non-blocking, evaluate/input bug fixes).
2. **Network expectation/interception API layer** (`expect_*`, `intercept`) as optional modern surface.
3. **Keyboard subsystem modernization** (`keys.py` approach + emoji/grapheme handling).
4. **Config additions** (`browser` selector including Brave, WebRTC/WebGL toggles, connection retry params).
5. **Documentation note**: formally document that zendriver currently emphasizes stronger testing/type tooling and additional convenience APIs (PDF/snapshot/base64 capture), while nodriver still contains some legacy helpers absent in zendriver.

## 8) Data collection notes

Analysis inputs included:

- project metadata (`pyproject.toml`) from both repositories
- AST-level method surface comparison for `core/browser.py`, `core/tab.py`, `core/element.py`, `core/connection.py`, `core/config.py`
- manual inspection of zendriver added modules (`expect.py`, `intercept.py`, `cloudflare.py`, `keys.py`)
- zendriver `CHANGELOG.md` entries up to `0.15.3` (2026-03-11)

