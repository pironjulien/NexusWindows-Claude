# One-step setup of NexusWindows for Claude on a fresh Windows PC, for people who never used a terminal:
#   irm https://raw.githubusercontent.com/pironjulien/NexusWindows-Claude/main/setup.ps1 | iex
# 1. installs uv (official release binaries, SHA-256 checked) when it is missing; uv then fetches Python 3.14 itself;
# 2. downloads the server from PyPI and checks that it loads (first start of Claude is then instant);
# 3. Claude Code: registers it (user scope), unless the nexus-windows plugin is enabled (then an entry this script
#    added earlier is removed: it would start a second server); Claude Desktop: declares it in
#    claude_desktop_config.json (merged, backup first, an entry of your own is kept).
# Nothing else is changed; run it again at any time (it only does what is missing).
# NEXUS_PACK=1 (Nexus pack installer): no question, no wait, no window; exit code 0 = done; last line
# "NEXUS_PACK_RESULT Claude Code=...; Claude Desktop=...".
$ErrorActionPreference = 'Stop'
$package = 'nexus-windows-claude'

function Say([string]$text, [string]$color = 'Gray') { Write-Host $text -ForegroundColor $color }

if ($env:OS -ne 'Windows_NT') { throw 'NexusWindows for Claude runs on Windows 10 or 11 only.' }

# 1. uv (https://docs.astral.sh/uv/): user install in ~\.local\bin, no administrator rights. Astral's official release
#    binaries, checked against their published SHA-256, then unpacked: no installer script is run (a child PowerShell
#    running a downloaded script is what antivirus heuristics flag).
$uvBin = Join-Path $HOME '.local\bin'
if (-not (Get-Command uvx -ErrorAction SilentlyContinue)) {
    if (-not (Test-Path (Join-Path $uvBin 'uvx.exe'))) {
        Say 'Installing uv (official release from GitHub, SHA-256 checked)...' Cyan
        $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'aarch64' } else { 'x86_64' }
        $asset = "uv-$arch-pc-windows-msvc.zip"
        $base = 'https://github.com/astral-sh/uv/releases/latest/download'
        $work = Join-Path ([IO.Path]::GetTempPath()) "nexus-uv-$PID"
        New-Item -ItemType Directory $work -Force | Out-Null
        try {
            $zip = Join-Path $work $asset
            $ProgressPreference = 'SilentlyContinue'  # Windows PowerShell 5.1 downloads far slower with its progress bar
            Invoke-WebRequest "$base/$asset" -OutFile $zip -UseBasicParsing
            $expected = ([Text.Encoding]::ASCII.GetString((Invoke-WebRequest "$base/$asset.sha256" -UseBasicParsing).Content) -split '\s+')[0]
            $actual = (Get-FileHash $zip -Algorithm SHA256).Hash
            if ($actual -ne $expected) { throw "uv download does not match its published SHA-256 ($asset): nothing was installed." }
            Expand-Archive $zip (Join-Path $work 'uv') -Force
            New-Item -ItemType Directory $uvBin -Force | Out-Null
            Get-ChildItem (Join-Path $work 'uv') -Recurse -Include 'uv.exe', 'uvx.exe', 'uvw.exe' | Copy-Item -Destination $uvBin -Force
        } finally { Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue }
        if (-not $env:UV_NO_MODIFY_PATH) {  # like Astral's installer: ~\.local\bin on the user PATH, for new programs
            $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
            if (($userPath -split ';') -notcontains $uvBin) {
                [Environment]::SetEnvironmentVariable('Path', (@($uvBin) + ($userPath -split ';' | Where-Object { $_ })) -join ';', 'User')
            }
        }
    }
    $env:PATH = "$uvBin;$env:PATH"  # this window
}
if (-not (Get-Command uvx -ErrorAction SilentlyContinue)) { throw "uv is installed but uvx was not found in $uvBin." }
Say "uv: $(uv --version)" Green

# 2. Server and Python 3.14 downloaded now (about 100 MB the first time), then the server module is imported.
Say "Downloading $package and Python 3.14 if needed..." Cyan
uvx --python 3.14 --refresh-package $package --from $package python -c 'import nexus_windows.server, nexus_windows; print(nexus_windows.__version__)'
if ($LASTEXITCODE) { throw "$package did not load: please open an issue at https://github.com/pironjulien/NexusWindows-Claude/issues" }
Say "$package ready." Green

# 3. Claude Code and Claude Desktop, by a short Python script run with uv's Python (JSON merged, files backed up).
$register = @'
import datetime, json, os, pathlib, shutil, subprocess, sys

NAME, PACKAGE = "nexus-windows", sys.argv[1]
ENTRY = {"command": "uvx", "args": ["--python", "3.14", PACKAGE], "env": {"PYTHONUTF8": "1"}}
home = pathlib.Path.home()
backups = home / ".claude" / "nexus-windows-setup-backups"


def ours(current):
    return current is None or (isinstance(current, dict) and PACKAGE in [str(a) for a in current.get("args", [])])


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() and path.stat().st_size else {}


def save(path, data):
    if path.exists():
        backups.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, backups / f"{path.parent.name}-{path.name}-{stamp}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".nexus-tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def plugin_enabled():
    try:
        enabled = load(home / ".claude" / "settings.json").get("enabledPlugins", {})
    except (OSError, ValueError):
        return False
    return any(key.split("@")[0] == NAME and on for key, on in enabled.items())


def claude_code():
    cli = shutil.which("claude")
    config = home / ".claude.json"
    current = load(config).get("mcpServers", {}).get(NAME) if config.exists() else None
    if plugin_enabled():
        if current is None or not ours(current):
            return "plugin"
        if cli:
            subprocess.run([cli, "mcp", "remove", NAME, "--scope", "user"], capture_output=True, check=False)
        else:
            data = load(config)
            del data["mcpServers"][NAME]
            save(config, data)
        return "plugin (earlier entry removed)"
    if not ours(current):
        return "kept (your own entry)"
    if current is not None:
        return "already declared"
    if cli:
        added = subprocess.run(
            [cli, "mcp", "add", "--scope", "user", "-e", "PYTHONUTF8=1", "--transport", "stdio", NAME, "--",
             "uvx", "--python", "3.14", PACKAGE], capture_output=True, text=True, check=False,
        )
        return "declared" if added.returncode == 0 else "failed: " + (added.stderr or added.stdout).strip()[:200]
    if config.exists():
        data = load(config)
        data.setdefault("mcpServers", {})[NAME] = {"type": "stdio", **ENTRY}
        save(config, data)
        return "declared"
    return "absent"


def folder_left(folder):
    return (p for p in folder.iterdir() if p.is_file() and p.name.startswith(".user-data-left."))


def claude_desktop():
    roaming = pathlib.Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    local = pathlib.Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    # Classic install, the Store (MSIX) ones (AppData redirected into their package), and the newer Store layout whose
    # data lives in %LOCALAPPDATA%\Claude-Data\<package>; a folder the app has left holds only a
    # ".user-data-left.<package>" marker and is skipped (it is no longer read).
    data = local / "Claude-Data"
    folders = [
        roaming / "Claude",
        *sorted((local / "Packages").glob("*Claude_*/LocalCache/Roaming/Claude")),
        *(sorted(f for f in data.iterdir() if f.is_dir()) if data.is_dir() else []),
    ]
    results = []
    for folder in (f for f in folders if f.is_dir() and not any(folder_left(f))):
        path = folder / "claude_desktop_config.json"
        data = load(path)
        servers = data.setdefault("mcpServers", {})
        if not ours(servers.get(NAME)):
            results.append("kept (your own entry)")
        elif servers.get(NAME) == ENTRY:
            results.append("already declared")
        else:
            servers[NAME] = ENTRY
            save(path, data)
            results.append("declared")
    return ", ".join(results) or "absent"


results = {}
for client, declare in (("Claude Code", claude_code), ("Claude Desktop", claude_desktop)):
    try:
        results[client] = declare()
    except Exception as error:  # noqa: BLE001 - one client's failure is reported, the other is still tried
        results[client] = f"failed: {error}"
print("NEXUS_PACK_RESULT " + "; ".join(f"{k}={v}" for k, v in results.items()))
sys.exit(1 if any(v.startswith("failed") for v in results.values()) else 0)
'@
$script = Join-Path ([IO.Path]::GetTempPath()) "nexus-windows-register-$PID.py"
[IO.File]::WriteAllText($script, $register)
try {
    $result = uv run --no-project --python 3.14 python $script $package | Select-Object -Last 1
    $code = $LASTEXITCODE
} finally { Remove-Item $script -ErrorAction SilentlyContinue }
if ($result -match 'Claude Code=(plugin|declared|already)') { Say 'Claude Code: ready.' Green }
if ($result -match 'Claude Desktop=(declared|already)') { Say 'Claude Desktop: ready.' Green }
if ($result -match 'Claude Code=absent' -and $result -match 'Claude Desktop=absent') {
    Say 'Neither Claude Code nor Claude Desktop was found: install one (https://claude.ai/download), then run this again.' Yellow
}
if ($env:NEXUS_PACK -ne '1') {
    Say ''
    Say 'Last step: close Claude completely (also from the notification area) and open it again,' Yellow
    Say 'then ask: "Open Notepad and write a shopping list: milk, eggs, bread."' Yellow
}
Write-Output $result
if ($code) { throw "Registration failed: $result" }
