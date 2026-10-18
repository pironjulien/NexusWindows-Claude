# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow the server's own version (`nexus_windows.__version__`).

## [0.8.2] - 2026-10-18

First public release since 0.7.0, prepared for PyPI and the official MCP registry (not on PyPI yet). It gathers the private 0.8.0 (shared guard-rail policy), 0.8.1 (suspended Store apps) and 0.8.2 (directories and
packaging) changes, plus the drop-down list fixes measured by the cross-agent bench of 2026-10-16.

### Added
- MCP tool annotations on every tool (`title`, `readOnlyHint`, `destructiveHint`, `idempotentHint`,
  `openWorldHint`).
- Startup outside Windows (`offwindows.py`): in the Linux container MCP directories such as Glama use, the server
  answers `initialize` and `tools/list`; every desktop tool answers that Windows is required (`logs` still reads
  the local log). `comtypes` and
  `windows-capture` are installed on Windows only. `tests/test_startup.py` runs on Windows and on Linux (new CI job).
- Shared guard-rail policy, one file for this server and NexusWindows for Antigravity (`garde-fous.json`, bundled in
  `src/nexus_windows/shared`): hard blocks (secure desktop, Windows credential prompts, password managers, now also
  for reading) and confirmation of irreversible gestures (`confirm: true` on the step: pay, buy, transfer, send,
  publish, delete permanently, format, uninstall, reset, cancel a subscription, delete an account, make a repository
  public), checked on what the gesture triggers (click by `[N]`, name or point, double-click, Enter, Ctrl+Enter,
  Space, Alt+letter, typed line break, DevTools pages). Missing or unreadable policy: every action refused.
- Store apps (UWP, WinUI) suspended by Windows because they never had the foreground are resumed with
  `IPackageDebugSettings.Resume`, without foreground nor persistent setting, at launch and before each `look` or
  `act` on them (`packages.py`).
- PyPI packaging (license, URLs, keywords, classifiers, `mcp-name` line) and a `publish.yml` workflow using Trusted
  Publishing, triggered by a GitHub release; GitHub Actions `tests.yml`; `glama.json`; README Privacy section.

### Removed
- Refusals with no safety value when the user already lets Claude run commands: consoles and Terminal, the Run
  dialog, taskbar, desktop, Start, Search, File Explorer, address bars, shell file dialogs and context menus, registry
  editor, MMC consoles, Task Manager, other AI agents and IDEs; at launch, interpreter, console, script-host and
  debugger lists. The local `policy.json` (`allow_processes`, `allow_shell`, `allow_launch`) is no longer read.
- Chromium switches that run a command are no longer refused; only those that open a browser session stay refused
  (`--remote-debugging-*`, `--load-extension`, `--disable-web-security`, a profile chosen by the model).

### Changed
- The Claude desktop app may be driven (no permission prompt reaches it in "bypass permissions" mode); the rule stays:
  never click one of Claude's own permission prompts.
- The handle-leak test warms up for 45 s before measuring (a one-off allocation was taken for a leak).
- `pyproject.toml`: English description, no author e-mail.

### Fixed
- Win32/WinForms drop-down lists without an ExpandCollapse pattern: `expand`, `collapse` and `click` on the list now
  open and close it with `CB_SHOWDROPDOWN`, read back with `CB_GETDROPPEDSTATE`; a list that does not open is `✗`;
  `CBS_SIMPLE` is refused.
- A choice made in an open WinForms list was lost behind a `~` (the application received an empty value): closing a
  list with `CB_SHOWDROPDOWN FALSE` is a cancel and restores the previous selection. The list is now closed first,
  then `CB_SETCURSEL`, then `CBN_SELCHANGE` and `CBN_SELENDOK` are sent (not posted) to the parent, then the
  application is read back (`CB_GETCURSEL`, `CB_GETLBTEXT`); an empty or other value is `✗`, never `~`.
- 22 s to report "no element" while a list was open: UI Automation chained from the open list to every top-level
  window of the desktop. The tree is now read level by level along the ancestors of the open list only, its options
  from the ComboLBox pop-up; target not found in an open list: 0.16 to 0.25 s (WinForms), 0.6 s (WPF).
- WPF: a list left open after a choice is closed and the choice read back; an option published twice by WPF is one
  target, not an ambiguity.

### Added
- `tests/test_fix_W.py`: 18 cases with a simulated ComboBox that cancels on close like Windows.
- README: drop-down lists, cross-agent bench results.

### Changed
- README renamed to NexusWindows for Claude, the Claude edition of the NexusWindows family: badges, banner, links
  to the other Nexus projects and to the project page (julienpiron.fr/nexuswindows). The Why section now states that
  Anthropic's own computer use is not available on Team and Enterprise plans, while this local MCP server is.

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
