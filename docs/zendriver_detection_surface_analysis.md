# nodriver vs zendriver: anti-detection implementation diff (code-focused)

## Scope
This document compares anti-detection-relevant implementation behavior between:
- **nodriver** (this repository)
- **zendriver** (`https://github.com/cdpdriver/zendriver`, cloned locally during analysis)

The focus is strictly on detection-surface changes, not general API/usability changes.

## High-impact anti-detection differences

### 1) WebRTC/WebGL hardening flags added in zendriver
In zendriver, `Config` introduces `disable_webrtc` and `disable_webgl` options and appends additional launch flags:
- `--webrtc-ip-handling-policy=disable_non_proxied_udp`
- `--force-webrtc-ip-handling-policy`
- `--disable-webgl`
- `--disable-webgl2`

These options are absent in nodriver's `Config` constructor/signature and argument assembly. The relevant zendriver lines are attributed (via blame) to commit **`529f335`** ("patch webRTC IP leaks + add disable WebGL option (#213)").

**Detection impact**:
- Likely **reduces proxy/IP leak exposure** (good for server-side correlation resistance).
- But optional WebGL disable may create a **stronger fingerprint anomaly** on sites expecting normal GPU/WebGL capability.

### 2) Cloudflare solving strategy changed (CV template matching -> DOM/shadow-root/iframe flow)
**nodriver** implementation (`Tab.verify_cf`) relies on screenshot + OpenCV template matching (`template_location`) to click challenge UI.

**zendriver** replaces this with a dedicated `core/cloudflare.py` strategy:
- searches DOM for shadow roots
- identifies `challenges.cloudflare.com` iframe signatures
- computes click coordinates from CDP box-model data
- supports selectors such as `input[name=cf-turnstile-response]` and fallback `input[name=cf_challenge_response]`

**Detection impact**:
- Potentially **less brittle** than screenshot template matching across localization/theme/resolution variance.
- More direct challenge-focused interaction patterns could become **heuristically recognizable** if reused with rigid timing/default click cadence.

### 3) Headless user-agent scrubbing remains, but implementation moved layers
In nodriver, headless and expert preparation helpers are in `Tab` internals. In zendriver, equivalent prep logic is handled in `Connection` (`_prepare_headless` and `_prepare_expert`) and triggered during `send()`.

Behavior that remains anti-detection relevant:
- reads `navigator.userAgent`
- overrides UA by removing `"Headless"`

**Detection impact**:
- Net stealth effect appears **roughly equivalent** in intent (remove explicit headless token).
- Architectural move likely improves consistency of when patching is applied, but does not by itself eliminate modern fingerprint checks.

### 4) New configurable user-agent override surfaces
zendriver adds:
- `Config(..., user_agent=...)` path (startup-level override)
- `Tab.set_user_agent(...)` helper

nodriver does not expose these in the same form.

**Detection impact**:
- Can improve realism if operators align UA with platform/browser build/proxy geography.
- Can **degrade stealth** if misconfigured (UA inconsistent with TLS fingerprint, client hints, platform APIs).

## Commit-level notes (zendriver)
From zendriver history and blame for anti-detection-relevant lines:
- **`529f335`**: "patch webRTC IP leaks + add disable WebGL option (#213)" (explicit anti-detection/privacy hardening intent)
- **`1ee5724`**: argument casing fixes around new config options (`disable_webrtc`/`disable_webgl` naming consistency)
- historical inherited commit references visible in blame for headless/expert logic origins, with current implementation now centralized in `Connection`.

## Detection-surface assessment by detector type

### Heuristic analysis systems
- **Improvement**: fewer accidental leaks (WebRTC) and more deterministic challenge handling.
- **Risk**: repeated fixed delays/click rhythm in challenge solver may still score as automation-like behavior.

### Client-side fingerprinting
- **Improvement**: headless UA token stripping still present.
- **Risk**: disabling WebGL can be a high-signal anomaly; custom UA mismatches can increase entropy/inconsistency.

### Server-side behavior monitoring
- **Improvement**: reduced true IP leakage through WebRTC in proxied workflows.
- **Risk**: challenge interaction sequences may appear too deterministic if defaults are not randomized.

### Common anti-bot frameworks (Cloudflare/DataDome/PerimeterX-style)
- **Improvement**: explicit handling for Cloudflare interactive challenge structures likely increases success rate on that class of flow.
- **Risk**: framework-specific hardcoded logic can age quickly and may become signatured if behavior remains static.

## Bottom line
Relative to nodriver, zendriver introduces meaningful anti-detection changes that are **mostly positive for operational stealth robustness** (notably WebRTC leak control and more structured Cloudflare challenge handling). However, some added toggles and convenience features can **increase detectability if used naively** (especially blanket WebGL disable or inconsistent UA overrides).
