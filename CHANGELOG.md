# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow the server's own version (`nexus_windows.__version__`).

## [Unreleased]

## [3.7.3] - 2026-10-28

### Fixed
- A single key with no proof asked for is now verified by an effect that only the key can produce: the keyboard focus
  on an element or a window that did not exist before (Explorer rename field after F2 or Ctrl+Shift+N, content opened
  by Enter; in the background the application thread's own focus is read, since a covered window never has the UI
  Automation focus), or, for Ctrl+A without a target, more items selected than before. A key sequence, a key aimed at
  a named target (its preparatory focus can create a field or select the target), a title, a value or a newly enabled
  button never count: the outcome stays unknown and the batch stops, as before.
- [N] numbers of a dialog seen as a popup of its parent window stay valid when the dialog is then targeted by its own
  title; the most recently shown list of the two wins.
- A scroll at a point with no scrollable container read under it (Settings page still loading after a click) is
  reported as a missing target, not as "impossible in the background".

## [3.7.2] - 2026-10-27

### Fixed
- Guardrails: the lowercase look-alikes в, к, м, н, т (Cyrillic) and β, η (Greek), dropped by mistake from the
  detection table in the previous release, are back ("Coмmander", "Posтer" are recognised again); a test checks every
  letter of both tables and that the set can no longer shrink.

## [3.7.1] - 2026-10-27

### Fixed
- Guardrails: an invisible format character (zero-width space, soft hyphen, direction mark, variation selector) or a
  Cyrillic or Greek look-alike letter no longer lets a word that needs confirmation slip through ("Env\u200boyer"
  displays as "Envoyer"). Look-alikes count for detection only.

### Security
- CodeQL code scanning; release files (the wheel and sdist PyPI serves) attached to each GitHub release with a
  Sigstore attestation and SHA256SUMS.txt (`gh attestation verify <file> --repo pironjulien/NexusWindows-Claude`);
  SECURITY.md states response times and coordinated disclosure; the last workflow step that piped a download into
  Python now reads a file.

## [3.7.0] - 2026-10-27 — Tk mouse wheel in the background

### Added
- Mouse wheel on a covered Tk window, without the foreground or the real mouse (until now no background technique
  existed, in any tool we measured). Tk sends the wheel to the window visible at the point of the message
  (tkWinX.c), so on a covered window it was lost. A Tk window that is ENTIRELY covered is moved beyond the virtual
  desktop (no activation, no z-order change), the wheel is posted to the same control there, and the window comes
  back to its place, read back to the pixel. Coverage is exact: the target frame minus the frame of each window above
  it; only windows proven opaque count (not layered, transparent, without redirection bitmap or shaped by
  SetWindowRgn), with rounded DWM corners left out at their monitor's scale; a one-pixel visible strip is enough to
  refuse. Also refused, nothing sent: maximized or minimized window, another control under the point once moved,
  window brought forward by the user meanwhile. A window that cannot be put back fails the step and stops the batch.
  Benchmark: Tk wheel 3/3 on a covered window, no foreground taken, cursor untouched.

## [3.6.1] - 2026-10-27 — Invoked on slow WPF windows

### Fixed
- On a WPF window that is slow to read (1.6 s per accessibility tree read in our lab), the "Invoked" event arrived
  during the read that followed the click, after the deadline, and was no longer looked at: buttons stayed `~`. The
  event is now awaited 0.15 s before any tree read, then checked again before concluding. Lab WPF button: `✓` in
  1.9 s instead of `~` in 3.9 s.

## [3.6.0] - 2026-10-27 — Proven clicks

### Added
- A click or menu choice without `expect` or `check` is proven on its own when the clicked element allows it, instead
  of ending `~` (46 % of our steps were `~` over three days, almost all of them clicks that had worked):
  - the UI Automation "Invoked" event that the application itself raises when the control receives the click,
    listened to on that element only (target by name, `[N]`, `x,y` point or menu entry), from its resolution to the
    end of the step. Raised by WPF and by Chromium/Electron. **WinForms (.NET Framework) and Win32 do not raise it**
    (measured, neither through UI Automation Invoke nor `BN_CLICKED`): their buttons stay `~` without `expect`. The
    report says the click arrived and that its result is not read (`expect` checks it);
  - the element's own state, tracked by its UI Automation runtime id in a complete and fresh read: checkbox checked
    or unchecked, item expanded or collapsed, list item or tab selected.
  Any other change (title, another element, a value, an element gone, a window opened or closed) is only reported:
  a clock, a notification or a streaming reply changes a window too. No extra read before the action; a WPF button
  now ends `✓` in about 130 ms instead of `~` after 0.8 s. In our benchmark, the Electron button went from 3/3
  uncertain to 3/3 `✓`, with no false success.

### Fixed
- A click the control acknowledged ("Invoked") is never sent again by a fallback technique, with or without
  `expect`/`check`, including the opening of an intermediate menu entry (which a second input would close).
- A click by messages on a covered Chromium, Electron or CEF window started without the occlusion switches said
  "the application probably ignores these messages". Its frozen image cannot show any effect: the report now says
  "sent; unverifiable: window covered and not redrawn", gives the remedy and does not invite to send it again.

## [3.5.0] - 2026-10-27 — Sharper images

### Changed
- Images sent to the AI follow the common Nexus image rule of 27/10: long side up to 2000 px and 2.56 MP (was 1568 px
  and 1.15 MP), lossless WebP while it fits 500 KB (was 200 KB, so a real screen went lossy), then quality 90, 80,
  70…; never over 500 KB. Small text is read better (98.8 % of characters instead of 92 % in our benchmark).

### Added
- `server.json`: `icons` (Nexus family medallion, PNG and SVG) so the MCP Registry and its clients show the
  product icon.

## [3.4.0] - 2026-10-27 — Structured output

### Added
- Structured output (MCP `outputSchema` and `structuredContent`) for the tools that return data: `windows` (hwnd,
  title, process, UI family, size, states, owner of a dialog), `notifications` (app, arrival time in ISO 8601, texts
  with security codes masked) and `logs` (version, pid, server state, call records). The text the model reads is
  unchanged; `look`, `act`, `launch` and `window` keep their text and image report without a schema.

### Changed
- README: the NexusWindows medallion at the top.

## [3.3.2] - 2026-10-26 — Nexus family icon

### Changed
- Claude Desktop extension icon (`mcpb/icon.png`, 512 px): the NexusWindows screen pictogram in the julienpiron.fr
  medallion, the same on the site and in every Nexus product. The busy indicator keeps its spinning π logo.

## [3.3.1] - 2026-10-26 — Documents opened by Store apps

### Fixed
- Opening a document whose default app is a Store app (for example an MP4 with Media Player) did nothing: no window,
  "opening NOT verified". The default app is now read from the file association (its AppUserModelID) and activated
  for the file with `IApplicationActivationManager::ActivateForFile`, which gives its process; apps without that
  contract (Photos, Notepad) still open through ShellExecute. Their window is recognized by its process (the frame is
  titled after the app, "Media Player", not after the file) and the document name is read in its UI Automation tree;
  a CoreWindow first shown hidden is followed into its visible frame; suspended content is resumed as for any Store
  app. When no window comes, the note says why and suggests opening it in the foreground with the user's consent.
  Checked on a real MP4: cold start (new visible window, name read back, foreground given back, 1.4 s) and player
  already open (2 s).
- A reused window that already was the user's foreground window is no longer reported as "foreground NOT given back".
- UI Automation tree read again (3 tries, 0.1 then 0.2 s) when Electron or Edge answers `EVENT_E_ALL_SUBSCRIBERS_FAILED`
  while rebuilding its accessibility tree, instead of reporting the window unreadable; any other error is still
  immediate.

### Changed
- Bench: `bench/cua_vs_nexus.py` checks for the user's return before every attempt and every Windows-MCP gesture;
  the Electron lab keeps drawing when covered; the WinForms and WPF lab windows are created off screen, put at the
  bottom of the window stack, then placed.

## [3.3.0] - 2026-10-26 — Updates by the server itself

### Added
- Update check by the server itself: at startup, in the background and at most once a day, it reads the latest
  version from PyPI and, when it was started by `uvx`, prepares it in uv's cache for the next start of Claude
  (`uvx nexus-windows-claude@<version> --version`; a `--version` flag prints the version without starting a server).
  Its state is written to `%USERPROFILE%\.nexus\status\windows-claude.json` (installed and latest version, `idle`,
  `checking`, `updating`, `done` or `failed`, message), the common contract of the Nexus apps. A failure keeps the
  running version. `NEXUS_UPDATE_CHECK=off` turns it off. The README's Privacy section lists this request. Installs
  declared without a version (`uvx --python 3.14 nexus-windows-claude`, as `setup.ps1` does) also start offline from
  uv's cache.

### Changed
- Lab and bench (`lab/apps.py`, `bench/cua_vs_nexus.py`): the user's physical input is watched for the whole bench;
  at the first one, every lab window and its cover are closed at once and the bench stops cleanly. Lab windows and the
  cover are put at the bottom of the window stack (a window shown without activation still lands on top), never over
  the user's windows.
- `setup.ps1` installs uv from Astral's official release binaries, checked against their published SHA-256, instead of
  running Astral's installer script in a child PowerShell (`-ExecutionPolicy Bypass -Command "irm … | iex"`), a pattern
  that antivirus heuristics flag. Same place (`~\.local\bin`, added to the user PATH unless `UV_NO_MODIFY_PATH` is
  set). README: the manual route installs uv with winget.

## [3.2.2] - 2026-10-25 — Common bench fixes

### Fixed
- `act` `scroll` in WPF: one notch is one UI Automation step again. Three small steps per notch reached the
  application as three separate scrolls (common bench of October 25: three `ScrollChanged`, counted as a false
  success). XAML (Settings, WinUI) still follows the Windows lines-per-notch setting, measured and set in one call.
- `act` `scroll` in Chromium apps without DevTools: when the scrolling area under the point is already at its end in
  that direction, the scroll is refused instead of moving the page around it. The page used to scroll in its place and
  the step was reported `✓` (common bench, Electron).

### Changed
- `mcpb/`: the extension follows the server by one release, since its lock file can only pin a version already on
  PyPI; its test checks the extension's own consistency.

### Fixed
- `setup.ps1`: recent Store installs of Claude Desktop read their configuration from
  `%LOCALAPPDATA%\Claude-Data\<package>`; those folders are now declared, and a folder the app has left (it holds a
  `.user-data-left.*` marker) is skipped. Existing keys such as `preferences` are kept.

### Added
- `mcpb/`: Claude Desktop extension (`.mcpb`, manifest 0.4, `uv` server type so Claude Desktop manages uv; the PyPI
  server pinned to the same version; Python 3.14). Validated with `@anthropic-ai/mcpb`; its launch command, run cold
  in an empty folder, starts the server (7 tools). The packed file is attached to the release. Installation inside
  Claude Desktop is not verified yet. `tests/test_mcpb.py` keeps its version, pin and tool list aligned.

### Changed
- `setup.ps1`: declares the server in Claude Desktop's `claude_desktop_config.json` (classic and Store installs;
  merged, backup in `~\.claude\nexus-windows-setup-backups`, an entry of your own kept) instead of only printing it.
  With the `nexus-windows` Claude Code plugin enabled, no `claude mcp add` (two servers otherwise) and an entry the
  script added earlier is removed. `NEXUS_PACK=1` (Nexus pack installer): no question, no window, exit code 0 or 1,
  last line `NEXUS_PACK_RESULT Claude Code=…; Claude Desktop=…`. Tested on an empty profile: Desktop only, rerun,
  Claude Code added, then the plugin enabled (one server, connected).

## [3.2.1] - 2026-10-25 — File picker wait

### Fixed
- File picker triggers in Chromium apps: the dialog is now awaited for up to 4 s per technique (measured at more than
  1.6 s after a background UIA Invoke in Edge). A shorter wait could report `✗` while the dialog was still coming, and
  a retry with `foreground=true` would then open a second one.

## [3.2.0] - 2026-10-25 — Dialogs listed, file pickers proven

### Added
- `windows` lists dialogs and other owned windows (a file picker "Open" or "Save as" owned by Chrome, for example),
  attached to their owner: `[dialog of <hwnd> "owner title"]`. A dialog can be targeted by its title; a process name
  still targets the main window first. Seen on October 25 in Chrome: the "Open" dialog was missing from the list and
  had to be targeted by its hwnd.
- `setup.ps1`: one-step setup for a fresh PC (`irm …/setup.ps1 | iex` in Windows PowerShell): installs uv with
  Astral's official installer when it is missing, downloads the server and Python 3.14 and checks that it loads,
  registers it with Claude Code (user scope) when the `claude` command exists, and prints the Claude Desktop
  configuration. Idempotent. Tested on an empty profile without Python, uv or Claude Code, then with Claude Code:
  `claude mcp list` reports the server connected.

### Fixed
- `act` `click` on a file picker trigger in a Chromium app ("Choose file", "Browse", "Upload", a drop zone; English
  and French labels): it now succeeds only when a dialog of the target opens (`✓ file dialog "Open" opened`, waited
  for up to 2 s, DevTools tried first when available since it is trusted input). Chromium opens a file picker only on
  a real user click; a hover or focus change used to be reported as a "visible change". Without a dialog: `✗` (or `~`
  when a posted click may still act) "no file dialog opened", suggesting `foreground=true` with the user's consent.
- Confirmation guard: a line break typed in a field is no longer taken for Enter on a nearby help button ("Why can't
  I submit this item?"). A neighbour counts only when it is a button with a command label (at most five words, no
  question mark), in UI Automation and in DevTools pages alike; a messaging composer next to "Send" stays guarded.
  The refusal or accord summary now names the field (`line break typed in "Description*"`).

### Changed
- README: installation rewritten for beginners (one-step script, Claude Code plugin, manual route with the official
  uv installer; Python is not a prerequisite; where Claude Desktop's configuration lives; first start about 100 MB).

## [3.1.5] - 2026-10-25 — Maintenance

### Fixed
- Auto-reload: when the server cannot restart on its new code, the message now says to reinstall nexus-windows
  instead of pointing to an installer that is not part of this repository.
- Test suite: the hygiene check (control and replacement characters) scans this project only, no longer the folder
  around the checkout.

### Changed
- Bench reports go through a shared redaction step (`bench/redact.py`: user profile and Documents folder removed
  before writing); obsolete raw bench results removed (cited and latest ones kept), two retired probes removed.
- Bench and live tests aligned on the English reports (`read back ✓`, `Window N`, `the foreground is held by`).
- Embedded shared policy updated to its current upstream version (header only, no rule change).

## [3.1.4] - 2026-10-24 — Right-click in File Explorer

### Fixed
- `act` `right_click` on a list or tree item (outside Win32/WinForms): like a real right-click, an unselected item is
  first selected alone (UIA Select, read back), an item already selected keeps the current selection; selection not
  read back or not published: no menu. UIA ShowContextMenu used to open the menu on the target ADDED to the selection
  (File Explorer, after going back with "Back": "Properties" covered two items). A right-click on a File Explorer
  display cell targets its row. Measured: "Properties of: alpha" with another folder selected before (was "2 items
  selected").

### Security
- Reviewed by the Codex agent over two rounds (unpublished selection state now refused).

## [3.1.3] - 2026-10-24 — Four bench fixes

### Fixed
- `act` `menu` on a classic Win32 menu bar (WinForms `MainMenu`): "File > Save as" now finds the "Save as..." entry
  (trailing ellipsis, case and spaces ignored; an exact match wins, a normalized one only when unique) instead of
  falling back to a failing UIA Expand. The `WM_COMMAND` is proven by the dialog it opens (a new captioned window of the
  target whose title carries the entry's label): "✓" instead of "~"; any other change stays "sent". The emergency stop
  is read again right before the post, and a command that may have left is never replayed through UIA.
- `act` `click` on a display cell ("Name", "Size"...) of a File Explorer item: the row alone is selected (UIA Select),
  like a real click. UIA SetFocus on the cell used to add the item to the selection ("2 items selected"). Limited to
  DirectUI display cells without a window of their own: a real editor (Win32 rename box, WPF grid cell) stays a field,
  a disabled cell is refused. A click through UIA Select is now read back ("selected").
- `act` `scroll`: one notch follows the Windows setting of its axis ("lines to scroll", 3 by default, or one page;
  "characters" horizontally) instead of a single small step; set to 0, nothing is sent. In XAML (Settings, WinUI),
  which animates every step and swallows the next ones (15 sent, 5 applied), the first step is sent alone and measured
  once settled, in the provider's own units, then the rest is set in one call (`SetScrollPercent`). An unreadable,
  unsettled or reversed measurement, or content that changes size meanwhile, is never extrapolated: the outcome is
  reported as unknown and nothing is replayed over the first step. Measured in Settings > Display: 5 notches,
  0 % → 24.3 % (was 0.3 % per notch).
- `window`, `look`, `act` given the HWND of a child window (a Store app's CoreWindow inside its frame) resolve to its
  top-level window: the Calculator now closes through its content HWND (was "still there").

### Security
- Reviewed by the Codex agent over five rounds (menu proof, cell scope, stop checks, scroll units and partial outcomes).

## [3.1.2] - 2026-10-24 — Text first

### Changed
- `look` (`show`), `act` and `launch` (`then`) default to the new `auto` mode: the element list, plus the image only
  when the tree is too thin to act on (fewer than 6 elements, a page that publishes nothing or may be stale, an
  unreadable or truncated tree) or when an `act` step ends `~` or `✗`. `both` keeps the previous behaviour. Measured
  on the common Windows bench (two runs, order swapped): same score with and without images (7/7), 77 % and 82 % fewer
  observation tokens.

### Security
- Reviewed by the Codex agent over two rounds (stale Chromium tree and truncated tree now attach the image).

## [3.1.1] - 2026-10-24 — Alt+F4 in the background on every window

### Fixed
- `act` key `alt+f4` with no target now closes any window through `WindowPattern.Close` (or `WM_CLOSE` without that
  pattern), read back and never replayed. Before, only WPF/UWP/WinUI did: on a Win32 dialog (Character Map) the posted
  key had no effect ("~ no visible effect"). Measured: closed in 76 ms, foreground untouched.
- Same guards as `window close`: a File Explorer window holding several tabs is never closed as a whole, a window
  disabled by an open modal dialog is refused, nothing sent; the emergency stop is read again right before the request.
  Tk and Chromium keep their own path (Tk's modal grab guard; DevTools and simulated activation).

### Security
- Reviewed by the Codex agent over two rounds (stop between the pattern lookup and the close request, Tk grab bypass,
  Explorer tabs).

## [3.1.0] - 2026-10-24 — Common image rule, Chromium apps launched covered

### Changed
- Images are WebP: lossless up to 200 KB, then quality 85, 70… until the image fits 500 KB, at the same size, so
  coordinates do not change (a 1429×804 screen: 48 KB instead of 84 KB in PNG). `detail="high"` follows the same
  1568 px / 1.15 MP limit (it was 2000 px); the `screen` overview also keeps under 1.15 MP. A zoom (`region`) is bounded
  by both the long side and the area.

### Added
- No duplicate image: when the exact pixels and frame are unchanged since the last image sent for that window or zoomed
  area, the image is not attached again and the reply says so; `show="image"` always attaches it. The image is marked
  as sent only once it is attached; debug copies (`NEXUS_WINDOWS_DEBUG_IMAGES=1`) never block a reply and are pruned
  like the logs.
- `launch` of a Chromium/Electron app by its Start menu name (custom AppID, e.g. Logi Options+): the shortcut's
  executable is started with the switches that keep it drawn while covered. A shortcut with its own arguments is kept
  as is, and `launch` says the switches are missing.
- `look` on a Chromium window whose captures are all uniform and that an opaque window fully covers (proven by the
  z-order) says it is "probably not drawn", with the remedy.

### Security
- Reviewed by the Codex agent over three rounds (zoom area over the pixel budget, a suppressed duplicate that could
  replace the coordinate frame, image prints left after a window was evicted, a debug write failure after the image
  was marked as sent, red and green with the same grey).

## [3.0.2] - 2026-10-24 — Tk in the background, scrolling without a wheel

### Added
- Tk in the background, window covered: clicks, right clicks and double clicks are sent as messages with the mouse
  button marked pressed for Tk's own thread only (Tk reads the button state with `GetKeyState` and ignores a posted
  click without it); keys and shortcuts get a simulated focus (`WM_SETFOCUS` sent to that window only, never
  `WM_ACTIVATE`, Ctrl/Alt marked pressed for its thread, `WM_KILLFOCUS` posted after the keys). The user's foreground,
  cursor and keyboard are untouched (`bench/miss_probes.py`).
- Scrolling without a wheel, always read back: in a covered Chromium or Electron page without DevTools, the next hidden
  line of the area under the point is brought into view (UI Automation ScrollItem), proven by the line's offset inside
  its area (the page around it may scroll too); in Tk, line messages go to the native scroll bar attached to the
  targeted widget (refused if that bar also touches another widget), proven by `GetScrollInfo`. Not moved: `~`.
- Bench task "scroll" (a list that scrolls, not the canvas of the "wheel" task): 3/3 on Tk, Electron and Edge.

### Changed
- Simulated Ctrl/Alt are held 0.5 s for every family (0.15 s sometimes let a covered Win32 window read Ctrl+K as K).
- A click on a point handed to the element under it is aimed at that point, not at the element's centre, and the
  element is read again first: it must still cover the point.

### Security
- Reviewed by the Codex agent over several rounds: once part of an input was accepted and the rest failed, the outcome
  is unknown and nothing else is tried on top (no other technique, no real input); the emergency stop is checked after
  each preparation, between keys and before each press; a Tk window in the foreground, or taken by the user during the
  action, gets nothing simulated; only the simulated keys are restored (their physical state if the user took the
  window); a simulated press is owned by a helper thread that always sends the release, one at a time per
  application, and an application whose release got no answer gets no further simulated press.

### Unchanged (measured, still refused)
- WPF: a click is dropped while the real cursor is not over the window (`HwndMouseInputProvider`); Ctrl+K would need
  UI Automation focus, which takes the foreground. A wheel on a covered Chromium or Tk canvas is routed to the window
  on top (`RerouteMouseWheel`, `WindowFromPoint`) and lost. A covered Electron page that has gone `hidden` renders
  nothing and ignores mouse input at a point: reported as `~`.

## [3.0.1] - 2026-10-23 — Family naming

### Changed
- Published on PyPI as `nexus-windows-claude` (`uvx --python 3.14 nexus-windows-claude`), like `nexus-windows-codex`
  and `nexus-windows-antigravity`; the server announces itself as `nexus-windows-claude`, titled "NexusWindows for
  Claude". Official MCP registry name: `io.github.pironjulien/nexus-windows-claude`.
- The former PyPI name `nexus-windows` stays available: version 3.0.1 only installs `nexus-windows-claude`, and the
  `nexus-windows` command still starts the server, so existing configurations keep working. The former registry
  entry `io.github.pironjulien/nexus-windows` is marked deprecated and points to the new name.

### Fixed
- README: the `claude mcp add` commands put `-e` before the server name (the variadic option swallowed it).

## [3.0.0] - 2026-10-23 — Common milestone of the NexusWindows family

The three NexusWindows tools (Claude, Codex, Antigravity) move to 3.0.0 together: one-command install from a package
registry, the same five tool names (`windows`, `look`, `act`, `launch`, `window`), the same step outcomes
(✓ verified, = already done, ~ unknown, ✗ failed or refused), and from now on one version number for the family.

### Changed
- Version 3.0.0, shared with NexusWindows for Codex and NexusWindows for Antigravity.
- README: the same plan as the two other tools, with a "NexusWindows family" section.

### Fixed
- Detailed log: expected text (`check.value`, `check.contains`, `expect`, `wait` text) and step error messages that
  quote typed or expected text are now replaced by their length, as the Privacy section promises; before, a
  postcondition could leave the typed text in the log.
- Detailed log: a step or call error is written as its first words only (cut before any quote, colon or bracket, as
  in the common journal) and a traceback without its message, so a refusal quoting typed text or field contents
  (truncated or normalized) never reaches the disk.

## [0.16.1] - 2026-10-23

### Security
- Pillow raised to 13.0.0 (minimum and lock): fixes the advisories reported against Pillow 12.3.0
  (one critical, six high).

## [0.16.0] - 2026-10-23

Second round of the common bench of the three NexusWindows tools (Claude, Codex, Antigravity): 7/7 tasks for each,
twice, target window covered.

### Added
- Common journal shared by the three NexusWindows tools: one short line per batch step (app executable, framework,
  action, technique, outcome, duration, foreground used), never any screen text, typed text, label, title or path.
  Local only, under `%LOCALAPPDATA%\NexusJournal\windows\claude\` (one file per month), for a monthly review.
- Mouse wheel over a Chromium/Electron/Edge window the tool did not launch, through DevTools: the port is read from the
  window's own process command line and used only when that exact process listens on it; the target page is proven
  by a random title seen in that window within 0.5 s (original title restored); no proof, nothing sent.

### Fixed
- Alt+F4 on WPF/UWP/WinUI windows in the background: closed through UI Automation WindowPattern.Close (WM_CLOSE only
  when the pattern is missing), read back; a close whose answer was lost is never replayed.
- A click that cannot be read back ("~") now reports what changed in the window within 0.8 s, never as a proof; only
  `expect` or `check` prove it. With `expect` on each step, a series of clicks runs in one call (~50 ms per click).
- Enter in a Win32 edit field that has its own window (Explorer rename): posted to the field without any brief
  activation, so it validates the name without also opening the folder.
- Window titles with non-breaking spaces match (exact title tried first).
- Wheel at a point covered by another app's window: the target window's scroll container under that point is used,
  only under a foreign window and never under the target's own popups.
- A key sequence only partly posted is reported as unknown and never replayed with the real keyboard.
- Posted click and double-click in DPI-virtualized windows: the point is converted to logical pixels (at 125 %, a
  double-click on a WinForms list landed one row too low).
- Posted double-click on a covered WinForms control is refused before sending (WinForms ignores it); the real mouse
  takes over only when allowed.
- Right-click is proven by a context menu actually opening; otherwise unknown. UIA ShowContextMenu with no menu
  opened falls back to the real mouse when allowed (WPF).
- Win32/WinForms: a posted mouse message with no visible effect is never replayed with the real mouse.

### Changed
- The "agent at work" indicator no longer has a halo: only the logo turns.
- Lab apps: horizontally scrolling areas, nested WPF scroll viewers, every web wheel event logged, for the bench.

## [0.15.9] - 2026-10-21

Measured by the common bench of the three NexusWindows tools (Claude, Codex, Antigravity): same lab apps, same tasks,
the application itself as the only judge (an effect received twice is a failure), target window covered, independent
observer of the foreground (Windows events + 1 ms sampling), cursor and clipboard.

### Fixed
- WinForms buttons (owner-drawn, `BS_OWNERDRAW`): `BN_CLICKED` is posted first, like Win32 push buttons. UI Automation
  Invoke is synchronous: a button that opens a modal dialog blocked it until the dialog closed (5 s lost, then "~").
  Modal dialog task: 13.6 s → 0.2 s.
- Right-click on Win32/WinForms: posted right-click first. It opens a WinForms context menu without touching the
  foreground (measured), and its command is then chosen by name; UIA ShowContextMenu opened nothing and its "~"
  stopped the chain. Context menu task: 0/2 → 2/2.
- Partial name matching never picks a page's root document by its title (the title changes with the page): a field
  briefly missing from a covered Chromium tree no longer resolves to the document.
- Wheel at a point over a canvas: when the scroll container is already at its end (nothing moved, no side effect), the
  wheel message at the point is tried next, then an explicit failure; no more "~" that stopped the chain.
- Shortcuts by messages: the simulated keyboard state is restored only once the target has processed its input
  (`WaitForInputIdle`, at most 2 s, at least the usual hold), so a slow target can no longer turn Ctrl+S into S.
- A batch stops after an action that was sent but could not be verified ("~"): the following steps are not sent, the
  action is never replayed, and the run cannot be saved as a successful routine. A requested postcondition that cannot
  be read stays unknown, even when the action itself was read back.
- `wait` and `expect` tell an incomplete read (unknown outcome, batch stopped) from text proven absent (failure).
- Two launch tests no longer read the real keyboard and mouse (a real Alt+Tab during the suite could fail them).

### Added
- Lab (WinForms, WPF): double-click on the list, a "Copier" context menu, a "Dialogue" button opening a real modal
  dialog, for the common bench.

## [0.15.8] - 2026-10-20

### Fixed
- Chromium/Electron without DevTools: the first background typing in an app launched a moment ago from the foreground
  program was lost ("0/30 read back") or a dropdown choice was not read back. Its first focus request uses the
  one-time foreground right Windows gives a freshly launched process; the guard hands the foreground back within
  ~10 ms, but Chromium then gets a real deactivation and drops the keys that follow. Now, the first time per process,
  the tool waits for that hand-back, sends the simulated activation again and takes the focus again before any
  character. Live suite: 14/14.

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
