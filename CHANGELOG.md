# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow the server's own version (`nexus_windows.__version__`).

## [0.7.0] - 2026-10-16

### Added
- Chromium/Electron simulated activation: typing (multiline fields, rich editors, `<select>`), keys and Ctrl/Alt
  shortcuts in an app already open without DevTools, with no real activation (`WM_NCACTIVATE` + `WM_ACTIVATE` sent to
  that window only, DOM focus through MSAA `accSelect(TAKEFOCUS)`, posted deactivation). Measured 18/18 on Electron and
  Edge with 0 to 1 foreground take of at most 9 ms.
- Four outcomes per step (`✓`, `=`, `~`, `✗`) and a tally at the end of each `act`.
- Step postconditions (`check`): one element and its wanted state, read before and after the step.
- Verified routines: `act(remember=...)` and `act(routine=..., params=...)`, saved per executable, every step
  re-verified on replay; optional mirror folder (`NEXUS_ROUTINES_MIRROR`) without typed text in clear.
- `launch(app=<folder or document>)`: opening through the file association, then verified.
- Shared foreground token with other agents (`%USERPROFILE%\.nexus\foreground.lock`) and shared emergency stop
  (`%USERPROFILE%\.nexus\stop`, `stop.owner`).
- `policy.json`: `allow_shell` to allow the taskbar and File Explorer.
- Steam (CEF in SDL frames): readable and controllable while covered, clean restart, DevTools port on request.
- `act(foreground=true)` keeps the window in front for the whole batch and its final render.
- `bench/chromium_input_probe.py`, `bench/chromium_sim_live.py`, `bench/speed.py`, `bench/steam_probe.py`.
- `test.ps1`: offline check in one command, venv and caches outside the repository.

### Changed
- `actions.py` split into per-family modules (`act_base`, `act_win32`, `act_uia`, `act_chromium`, `act_pointer`,
  `act_wait`); `actions.py` stays the facade.
- The final render of an `act` waits until the window is still instead of a fixed 300 ms: 418 → 221 ms (Win32),
  341 → 205 ms (Electron).
- Chromium click: UIA Invoke or default action first, each technique read back before trying the next.

### Fixed
- `launch(msedge, devtools=true)` failed after 0.5 s and left an orphan browser when Edge relaunched itself.
- Covered Chromium windows published accessibility changes one action late; publication is now triggered before reads.
- Rich editors with empty paragraphs were read back as incomplete.
- Deterministic foreground guard while a Store app is launched.

## [0.5.0] - 2026-10-12

Previous public release (initial open-source export).
