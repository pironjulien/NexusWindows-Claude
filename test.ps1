# Full offline check: unit tests, lint and format. Exit code 0 when everything passes.
# The virtual environment, bytecode and caches live outside the repository; nothing is written to the source tree.
# -Full also runs the end-to-end tests on the lab windows (they refuse to run if you used the PC in the last 2 min).
[CmdletBinding()]
param([switch]$Full)
$ErrorActionPreference = 'Stop'
$state = Join-Path $env:LOCALAPPDATA 'claude-windows-computer-use'
$env:UV_PROJECT_ENVIRONMENT = Join-Path $state 'venv'
$env:UV_LINK_MODE = 'copy'
$env:PYTHONPYCACHEPREFIX = Join-Path $state 'cache\pycache'
Push-Location $PSScriptRoot
try {
    uv sync --frozen
    if ($LASTEXITCODE) { throw 'uv sync failed.' }
    uv run --frozen pytest -q -p no:cacheprovider
    if ($LASTEXITCODE) { throw 'Unit tests failed.' }
    uv run --frozen ruff check .
    if ($LASTEXITCODE) { throw 'ruff check failed.' }
    uv run --frozen ruff format --check .
    if ($LASTEXITCODE) { throw 'ruff format check failed.' }
    if ($Full) {
        uv run --frozen pytest -q -p no:cacheprovider -m live
        if ($LASTEXITCODE) { throw 'End-to-end tests failed.' }
    }
} finally { Pop-Location }
exit 0
