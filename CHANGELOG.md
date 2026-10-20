# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow the server's own version (`nexus_windows.__version__`).

## [Unreleased]

### Changed
- Side-by-side bench redone: nexus-windows 0.15.7 against Cua Driver 0.37.0, target covered before and after each
  attempt (38 tasks each): effect proven 29 against 17, false successes 0 against 3, foreground taken 1 against 7,
  median 211 ms against 706 ms. Counts computed by `bench/tally_cua.py`; `--nexus-only` mode. Live test: the Settings
  step targets `ListItem:Système` (a breadcrumb button has the same name).

## [0.15.7] - 2026-10-20

### Fixed
- Password fields: Shift+letter and shortcuts (Ctrl+V, Ctrl+A then Delete) are refused too; only Tab, Enter and Escape
  go through.
- DevTools: an input or textarea inside a rich editor is targeted before the editor around it; replacing its text used
  to replace the whole editor, surrounding text included. Checked in a real Chromium.
- Foreground: the holder read by the caller is handed to the give-back (a user switching windows between two reads is
  no longer covered); the give-back stops when the foreground no longer belongs to the target; an empty foreground
  followed by a user window counts as the user's choice.
- Real keyboard: focus read back after Ctrl+A before Delete, then before each 32-character chunk of typing.
- Simulated keys (Chromium) go to the targeted element's own top-level window (dialog, popup), not the main window.
- Virtualized lists: an exact name is required once the item is realized, never an approximate namesake.
- Capture: PrintWindow bounded to 3 s on its own thread even after a successful WM_NULL; WGC takes over.
- Notifications: a sign-in code is masked even without the word "code" ("Enter 739201 to sign in").
- Bench against Cua: occlusion required before each attempt; rows where the target was not covered are flagged
  outside the count.
- Emergency stop: the remaining time is clamped to [0, hold duration]; floating-point rounding could report 120.00000000000003 s.

### Changed
- Dependency updates: pyright 1.1.414 (dev), `uv_build` build backend allowed up to <0.14.0.

## [0.15.6] - 2026-10-20

### Fixed
- `launch`: the tool follows the window the user picks during the wait (click on a window or the taskbar, Alt+Tab,
  Windows key) and never takes it back; only a window that took the foreground without a gesture from the user is
  pushed back, to the user's current window. Applies to addresses, documents, Settings, Steam and the end of a launch.
- Web pages: a password field inside a shadow DOM is refused again (the active element is read in depth everywhere);
  a target designated as a password is refused before any focus. A fully editable page (body) is accepted again.
- Foreground give-back is judged window by window, not per process (Explorer or a browser run all their windows in
  one process); a new window of the target (splash screen, then main window) is followed.

## [0.15.5] - 2026-10-20

### Fixed
- Real keyboard (`foreground=true`, typing after a real click): only the effective foreground focus (UI Automation)
  counts; the focus remembered by the app is used only for addressed messages.
- DevTools: in a rich editor the outermost editing host is targeted and the selection must be inside it; shadow DOM
  traversed (point, ancestors, focus read back). Otherwise refused, nothing sent. Checked in a real Chromium.
- Foreground give-back: no fallback when the user took a window of another program; last check right before
  `SetForegroundWindow`; a momentary null foreground or another window of the same program no longer stops it.
- Regression of 0.15.4 removed: typing in the user's own window no longer suspends the give-back.
- Settings are never slid under the previous window when the user went there themselves.

## [0.15.4] - 2026-10-20

### Fixed
- Focus checked on the channel actually used: the app's own focus (the one that receives messages) or the UI
  Automation focus on the field or a descendant; read back briefly for Chromium (deferred focus).
- DevTools: a targeted key aims at the focusable element under the point, focus read back (Ctrl+A then Delete could
  empty the previous field of a rich editor).
- Long typing in chunks: focus read back before each chunk; typing stops rather than continue in another field.
- Known limit, refused honestly: a Ctrl shortcut aimed at a native field during a brief activation (simulated
  modifiers never reach the foreground thread, to protect the user's keyboard); `foreground=true` does it.

## [0.15.3] - 2026-10-20

### Changed
- `launch` is entirely free: the last guard (confirming `tel:`, `sms:`…) was bypassable (`explorer.exe tel:…`, `.url`
  file). The outward gesture ("Call", "Send") stays a click guarded by `confirm` and the exact accord in `act`. The
  `confirm` parameter is removed from `launch`.

### Fixed
- Clearing or replacing a field (messages, real typing): focus read back on the targeted field, or its inner field,
  before Ctrl+A and Delete; a field that vanished or was replaced stops everything.
- Address taken by an already open app: the user's input marker is taken at launch time; "foreground given back" is
  read back, never assumed.

## [0.15.2] - 2026-10-20

### Fixed
- The "agent at work" indicator module also loads outside Windows (no `ctypes.WINFUNCTYPE` there): 0.15.0 failed the
  Linux startup check of the PyPI release, so 0.15.0 and 0.15.1 never reached PyPI. Test added.

## [0.15.1] - 2026-10-20

### Changed
- `launch` is free again, as the shared policy says (consoles, interpreters and scripts allowed): the 0.15.0 list of
  programs and documents "to confirm" was cosmetic (npx, winget, java, Store apps with arguments went through). Only
  addresses that call or write to someone (`tel:`, `sms:`, `callto:`, `skype:`, `msteams:`…) need `confirm=true`.

### Fixed
- Address taken by an already open app: a dialog of one of the user's windows (owner chain) is no longer sent behind.
- Panels: a panel the user opens while we wait for their pause is read again just before the shortcut (the shortcut
  would have closed it); for a late panel, only the requested panel is closed, never another one the user opened; a
  short-lived toast (same process and class, ~100 px) is no longer taken for the notification center.
- Keys sent as messages follow the same rule as the real keyboard: the designated target must have the focus, read
  back; real typing by click reads the field's focus back before Ctrl+A and Delete.
- Virtualized lists: an ordinary list (without ItemContainer) no longer stops the search.
- Notifications: access codes ("access", "do not share"…) masked.

## [0.15.0] - 2026-10-20

### Added
- "Agent at work" indicator: while an action runs (`act`, `launch`, `window`; never the reads), the logo spins in the
  bottom-right corner of the driven window's screen with a breathing blue halo, and goes away 1.6 s after the last
  action. Always on top, borderless, click-through, never activated, not in the taskbar, excluded from screen
  captures. `NEXUS_WINDOWS_INDICATOR=0` turns it off.

### Changed
- `launch` asks for confirmation instead of a cosmetic block: interpreters and installers (cmd, PowerShell, msiexec,
  wscript, rundll32, py…), scripts, installers, shortcuts and links (by extension or by the program associated on the
  PC: `.py` → `pyw.exe`), and address schemes other than `ms-settings`, `mailto`, `http(s)` and `ms-windows-store`
  need `confirm=true`, given only on the user's explicit order. An address taken by an already open app: foreground
  given back, stop if the user touches keyboard or mouse.

### Fixed
- Whole screen (`look(window="screen")`): every window blocked by the policy (password managers, secure desktop),
  pop-ups included, is blacked out and its title never shown; areas read before and after the capture, captured
  again if they differ.
- Panels (Start, quick settings, notification center): closed only if they still hold the foreground (a user who
  moved on keeps their choice); a panel the user opened is never closed; a late panel is caught and closed.
  Shortcuts limited to Win, Win+A, Win+N.
- Real typing stops at once when the call is abandoned (timeout); real keyboard given to the designated target only
  (focus read back, otherwise no key).
- A minimized Store app is told apart from an app never shown.
- Notifications: codes at the end of a sentence, PINs, confirmation codes, Spanish and Italian wording masked; an
  account balance no longer is; expired notifications dropped before the read limit.
- Virtualized lists: search bounded in time (2 s). Screen capture without CAPTUREBLT (cursor flicker).

## [0.14.1] - 2026-10-20

### Fixed
- `notifications`: alphanumeric security codes are masked too ("K7F2QX", "ab12-cd34"); numeric XML entities are
  decoded before masking (a code written as `&#55;…` was shown in clear); reading is bounded to the 1,000 most recent
  toasts. Found by a review from Codex.

## [0.14.0] - 2026-10-20

### Added
- Store apps launched in the background (Settings, Calculator…) that Windows never shows are listed by `windows` as
  `[hidden]` and read and driven through their hidden CoreWindow: the whole Settings app, page after page, with
  nothing on screen. An empty UWP frame found at launch is replaced by that CoreWindow.
- `launch` opens addresses: `ms-settings:<page>` (that Settings page; foreground given back at once, window slid
  under the user's), `mailto:`, `https:`… Schemes that run, install or fetch something are refused (`file:`,
  `ms-msdt:`, `search-ms:`, `ms-appinstaller:`…).
- Windows itself: `window="taskbar"` (buttons, tray, clock) and `window="desktop"`, listed by `windows`;
  `look(window="screen")` shows the whole screen as the user sees it, with each window's place (overview only).
- `notifications` tool: Windows notifications read from their database (read-only), nothing opened on screen;
  security codes masked when the notification talks about a code or a sign-in (title included).
- Panels that only exist in the foreground: `act(window="quick settings" | "notifications" | "start",
  foreground=true)` opens them by their shortcut, keeps them open until the final render, then gives the foreground
  back (Escape when a panel refuses to give it up). Start is read in StartMenuExperienceHost, which `EnumWindows`
  does not list (immersive band).
- Virtualized lists (Settings, long lists): an exact name missing from the tree is found through ItemContainer,
  realized and scrolled into view.

### Fixed
- A frozen application no longer blocks the server: PrintWindow waits forever on a hung window (measured: more than
  8 s, then 180 s on a frozen Settings). Capture first checks that the window answers (bounded WM_NULL) and falls back
  to the screen-composition capture; `look` reports it.

## [0.13.4] - 2026-10-20

### Fixed
- The server starts again outside Windows (the Linux startup check of MCP directories and of the release build): the
  UI Automation `Text` type of OCR elements is the documented constant 50020 instead of a lookup in a table that is
  empty outside Windows. 0.13.3 never reached PyPI; 0.13.4 ships its changes below.

## [0.13.3] - 2026-10-20 (not on PyPI)

Versions 0.11.0 to 0.13.2 were not published separately; 0.13.3 ships all of them. Every fix below comes from a
review by another AI (Codex or Antigravity) that reproduced the defect first; each reproduction is kept as a test.

### Added
- `act(then="changes")` and `look(show="changes")`: only what changed since the last list shown for the window
  (`+` added, `~` changed with the reason, `-` removed); other elements keep their `[N]`. Full list when almost
  everything changed (60 %) or when there is no previous list. Idea taken from Cua Driver; 2 to 2.6 times shorter
  than the full list after a click.
- A common scroll is reported as one line naming the scrolled elements as ranges (`[2-21]`) instead of every element
  marked as moved.
- OCR fallback with the Windows built-in engine (`Windows.Media.Ocr`, local) when a window has fewer than 6 UI
  Automation elements or a Chromium page stops publishing its content: `[N] Text "…" (OCR)` elements with a
  position, targetable by number or name. 10 to 21 ms per window. Optional `winrt-*` dependencies (Windows only).
- Approval bound to the exact action: an outward gesture (send, call, pay, publish, transfer) needs `confirm: true`
  and a single-use `accord` token returned by a first refusal that summarises the exact action; any change voids it.
  Same check for every path, now including Win32 menus (`WM_COMMAND`).
- Text read on screen that looks like instructions aimed at an AI gets a warning appended (signal only).
- Bench side by side with Cua Driver 0.36.0 (`bench/cua_vs_nexus.py`, results in `bench/RESULTS.md`), background
  probes (`bench/background_probes.py`, `bench/chromium_wheel_probe.py`) and OCR bench (`bench/ocr_live.py`).

### Fixed
- Mouse wheel on an aimed point whose container is already at the end: `~` with the reason instead of a false `✓`.
- Tk buttons: no more `BN_CLICKED` or posted messages (Tk ignores them); clear refusal in the background.
- `then="changes"`: an element whose change was cut from a long report loses its `[N]`; a removed `[N]` is never
  given again (monotonic counter per window); moving the window is no longer reported as a scroll or as moves
  (comparison in the window's frame); an OCR target follows the window when it moves.
- Re-validating a target by `[N]` or name no longer alters the list used as the base of `then="changes"` (the base is
  frozen when shown), and targets keep their rank among siblings (duplicate options in a Win32 list, Win32 tabs).

## [0.10.1] - 2026-10-19

### Fixed
- The new client-text tests moved to `tests/test_english.py` (Windows only): in `tests/test_startup.py` they imported
  the desktop modules, which broke the Linux startup check of the release build, so 0.10.0 never reached PyPI.
  0.10.1 ships the 0.10.0 changes below.

## [0.10.0] - 2026-10-19 (not on PyPI)

### Changed
- Everything an MCP client receives is now in English: tool descriptions, step and parameter descriptions (JSON
  schemas), step reports, summaries, refusals, errors, warnings and the `logs` output. Same meaning, same
  guardrails; the symbols ✓ ~ ✗ = are unchanged. Inputs keep their French aliases (key names, role prefixes such as
  `bouton:`, scroll directions).
- Server instructions state the real password rule: typing into a password field is always refused; password
  managers and the secure desktop are never read nor driven (Windows credential prompts are no longer a hard block
  since 0.9.3; README aligned).
- The shared policy's error messages are in English.

### Added
- Startup tests: `tools/list` (instructions, descriptions, every schema description) and a series of offline error
  and refusal responses must contain no French; instructions and descriptions must not steer the model toward or
  away from other tools.

### Added
- `.github/workflows/registry.yml`: after each release reaches PyPI, `server.json` is published to the official MCP
  registry by GitHub OIDC (no token, no manual login), as NexusAndroid does.

## [0.9.3] - 2026-10-19 — Neutral texts for any client

### Changed
- Server instructions sent to every MCP client are now in English and neutral ("the user"): no personal name.
  Tool descriptions, field descriptions and messages returned to the model say « l'utilisateur » (they stay in
  French). Same rules, same guardrails. A test checks that nothing sent to a client names anyone.
- Policy test aligned with the shared guardrails of 2026-10-19: Windows credential prompts are no longer a hard
  block for Claude (password managers and the secure desktop still are).

### Fixed
- Memory: the Windows.Graphics.Capture fallback (used only when PrintWindow returns a blank image) loaded the GPU
  driver, numpy and OpenCV inside the conversation's server: +772 MB private memory and ~25 threads, never released.
  It now runs in `wgc_helper`, a helper process started on demand, restarted if it dies and stopped after 61.8 s
  idle or when the server exits (+14 MB on the server side).
- Chromium/Electron menu buttons (aria-haspopup, ExpandCollapse pattern): `click` opens them with Expand/Collapse
  first (Invoke only if nothing moved); `expand`/`collapse` accept a collapsible button too.
- A server started outside the interactive desktop saw no window: its thread now attaches to the « Default »
  desktop before listing windows or reading the foreground.
- Shared guardrails updated (« Réinitialiser » confirmed again except for harmless actions such as filters or zoom).

### Added
- README: animated demo recorded from the real tool (docs/assets/demo.webp).

### Changed
- Repository renamed to `pironjulien/NexusWindows-Claude`; GitHub redirects the old address.

## [0.9.2] - 2026-10-18 — No more freeze after a reload

### Fixed
- Supervisor: a call cancelled by the client (`notifications/cancelled`) is never answered by the server (MCP
  specification, SDK 2.3.0); it stayed "in progress" and the next reload waited for it forever, freezing every call.
  A cancellation now removes it from the calls in progress; cancelled while still queued, it is not sent (atomic
  queue → in-progress step, replays included).
- Waiting for calls in progress is bounded to 120 s: beyond that the server is taken as frozen, its calls get an
  "outcome UNKNOWN" answer and the reload starts a fresh server.
- Writes to the server child are serialised under a lock (dispatch thread and cancellation thread).
- Two rounds of adversarial review by Antigravity (races, poisoned ids, endless wait); 3 tests added.

### Changed
- Listed in the official MCP registry (`io.github.pironjulien/nexus-windows`); README installs from PyPI with
  `uvx --python 3.14 nexus-windows`.

## [0.9.1] - 2026-10-18

First release on PyPI (`nexus-windows`).

### Changed
- Shared guard-rail policy updated (nexus-shared 6d82bbb): erasing, resetting or formatting the whole device now
  needs confirmation too; "erase everything" counts only on a reset screen.
- Shared guard-rail policy updated (same file as the Codex, Antigravity and Android editions): calling ("appeler",
  "call") now needs `confirm: true` too.

### Fixed
- Supervisor: the reload note and the `tools/list` tracking are released before the result is checked, so nothing is
  left pending when the server child answers with a JSON-RPC error (review by Antigravity).

## [0.9.0] - 2026-10-18

### Added
- Automatic reload on new code, without `/mcp`: `python -m nexus_windows` and the `nexus-windows` script now start a
  stdio supervisor (`reloader.py`, standard library only) that relays the client's MCP link to a server child
  process. Before each tool call it compares the fingerprint of the code on disk with the running server's; when it
  changed, it waits for calls in progress, lets the disk settle, starts a fresh server and replays the client's
  handshake. The reply starts with a note ("nexus-windows rechargé …"; `[N]` numbers are forgotten). A fresh process
  rather than `importlib.reload`, so guard rails, emergency stop, foreground token and logs restart consistent.
- A tool list changed by the new code sends `notifications/tools/list_changed` (the `listChanged` capability is
  announced).
- A "stale server" refusal between the two checks is replayed on a fresh server (nothing had been done). A server
  that stops during a call: outcome UNKNOWN, restarted at the next call. Broken new code: a clear error with the end
  of its error output, nothing done, the next call retries.
- `logs`: an "automatic reload" line. `tests/test_reloader.py` (9 cases with a simulated child server).

### Changed
- `codeprint.py`: code fingerprint and shared-policy folder, without dependencies (used by the supervisor,
  `freshness` and `policy`).

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
