# One-step setup of NexusWindows for Claude on a fresh Windows PC, for people who never used a terminal:
#   irm https://raw.githubusercontent.com/pironjulien/NexusWindows-Claude/main/setup.ps1 | iex
# 1. installs uv (Astral's official installer) when it is missing; uv then fetches Python 3.14 by itself;
# 2. downloads the server from PyPI and checks that it loads (first start of Claude is then instant);
# 3. registers it with Claude Code when the `claude` command exists, otherwise prints what to paste in Claude Desktop.
# Nothing else is changed; run it again at any time (it only does what is missing).
$ErrorActionPreference = 'Stop'
$package = 'nexus-windows-claude'

function Say([string]$text, [string]$color = 'Gray') { Write-Host $text -ForegroundColor $color }

if ($env:OS -ne 'Windows_NT') { throw 'NexusWindows for Claude runs on Windows 10 or 11 only.' }

# 1. uv (https://docs.astral.sh/uv/): user install in ~\.local\bin, no administrator rights.
$uvBin = Join-Path $HOME '.local\bin'
if (-not (Get-Command uvx -ErrorAction SilentlyContinue)) {
    if (-not (Test-Path (Join-Path $uvBin 'uvx.exe'))) {
        Say 'Installing uv (Astral official installer)...' Cyan
        powershell -NoProfile -ExecutionPolicy ByPass -Command 'irm https://astral.sh/uv/install.ps1 | iex'
        if ($LASTEXITCODE) { throw 'The uv installer failed: see https://docs.astral.sh/uv/getting-started/installation/' }
    }
    $env:PATH = "$uvBin;$env:PATH"  # this window only; the installer already added it for new programs
}
if (-not (Get-Command uvx -ErrorAction SilentlyContinue)) { throw "uv is installed but uvx was not found in $uvBin." }
Say "uv: $(uv --version)" Green

# 2. Server and Python 3.14 downloaded now (about 100 MB the first time), then the server module is imported.
Say "Downloading $package and Python 3.14 if needed..." Cyan
uvx --python 3.14 --refresh-package $package --from $package python -c 'import nexus_windows.server, nexus_windows; print(nexus_windows.__version__)'
if ($LASTEXITCODE) { throw "$package did not load: please open an issue at https://github.com/pironjulien/NexusWindows-Claude/issues" }
Say "$package ready." Green

# 3. Claude Code: user scope, so every project sees it. Already registered: left as it is.
$claude = Get-Command claude -ErrorAction SilentlyContinue
if ($claude) {
    # Windows PowerShell 5.1 turns a redirected native stderr into a terminating error under 'Stop'.
    $ErrorActionPreference = 'Continue'
    claude mcp get nexus-windows *> $null
    $missing = $LASTEXITCODE -ne 0
    $ErrorActionPreference = 'Stop'
    if ($missing) {
        claude mcp add --scope user -e PYTHONUTF8=1 --transport stdio nexus-windows -- uvx --python 3.14 $package
        if ($LASTEXITCODE) { throw 'claude mcp add failed.' }
        Say 'Registered with Claude Code (user scope).' Green
    } else {
        Say 'Already registered with Claude Code.' Green
    }
}
Say ''
Say 'Claude Desktop: Settings > Developer > Edit Config, then put this in claude_desktop_config.json' Yellow
Say '(merge it into "mcpServers" if the file already has one), save, and quit Claude completely:' Yellow
Say @"
{
  "mcpServers": {
    "nexus-windows": {
      "command": "uvx",
      "args": ["--python", "3.14", "$package"],
      "env": { "PYTHONUTF8": "1" }
    }
  }
}
"@
Say ''
Say 'Last step: close Claude completely (also from the notification area) and open it again,' Yellow
Say 'then ask: "Open Notepad and write a shopping list: milk, eggs, bread."' Yellow
