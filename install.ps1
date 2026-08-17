<#
.SYNOPSIS
    Install Crystal Security Engine on Windows.

.DESCRIPTION
    Verifies Python >= 3.10, installs Crystal (with the tree-sitter extra by
    default), checks that `crystal` is reachable on PATH, and reports which
    optional external tools are present.

.PARAMETER Mode
    venv     create a dedicated .venv in the repo (default)
    user     install into the Python user site (pip install --user)
    system   install into the active interpreter as-is

.PARAMETER Editable
    Install in editable mode (pip install -e). Default for venv/user.

.PARAMETER NoTreeSitter
    Skip the tree-sitter extra; Crystal will run on the regex fallback parsers.

.PARAMETER WithSolc
    Install solc-select and a pinned solc build.

.EXAMPLE
    .\install.ps1
    .\install.ps1 -Mode user -WithSolc
#>
[CmdletBinding()]
param(
    [ValidateSet('venv', 'user', 'system')]
    [string]$Mode = 'venv',
    [switch]$Editable,
    [switch]$NoTreeSitter,
    [switch]$WithSolc,
    [string]$SolcVersion = '0.8.20'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Write-Ok($message) { Write-Host "    ok   $message" -ForegroundColor Green }
function Write-Warn($message) { Write-Host "    warn $message" -ForegroundColor Yellow }
function Write-Fail($message) { Write-Host "    FAIL $message" -ForegroundColor Red }

Write-Host ""
Write-Host "Crystal Security Engine - installer" -ForegroundColor White
Write-Host "-----------------------------------"

# --- 1. Python -------------------------------------------------------------
Write-Step "Checking Python"
$python = $null
foreach ($candidate in @('python', 'python3', 'py')) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $found) { continue }
    $raw = & $found.Source -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $raw) { continue }
    $parts = $raw.Trim().Split('.')
    if ([int]$parts[0] -gt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10)) {
        $python = $found.Source
        Write-Ok "$python (Python $raw)"
        break
    }
    Write-Warn "$($found.Source) is Python $raw (need >= 3.10)"
}
if (-not $python) {
    Write-Fail "No Python >= 3.10 found. Install it from https://www.python.org/downloads/ and re-run."
    exit 1
}

# --- 2. Target environment -------------------------------------------------
$pipTarget = @()
switch ($Mode) {
    'venv' {
        $venv = Join-Path $root '.venv'
        if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
            Write-Step "Creating virtual environment at $venv"
            & $python -m venv $venv
            if ($LASTEXITCODE -ne 0) { Write-Fail "venv creation failed"; exit 1 }
        }
        $python = Join-Path $venv 'Scripts\python.exe'
        Write-Ok "using $python"
    }
    'user' { $pipTarget = @('--user') }
    'system' { }
}

Write-Step "Upgrading pip"
& $python -m pip install --upgrade pip --quiet @pipTarget
if ($LASTEXITCODE -ne 0) { Write-Warn "pip upgrade failed; continuing" } else { Write-Ok "pip current" }

# --- 3. Crystal ------------------------------------------------------------
$extras = if ($NoTreeSitter) { '' } else { '[treesitter]' }
$spec = ".$extras"
$editableFlag = @()
if ($Editable -or $Mode -ne 'system') { $editableFlag = @('-e') }

Write-Step "Installing crystal ($spec)"
Push-Location $root
try {
    & $python -m pip install @editableFlag $spec @pipTarget
    if ($LASTEXITCODE -ne 0) {
        if (-not $NoTreeSitter) {
            Write-Warn "tree-sitter extra failed; retrying core-only install"
            & $python -m pip install @editableFlag '.' @pipTarget
            if ($LASTEXITCODE -ne 0) { Write-Fail "install failed"; exit 1 }
            Write-Warn "installed without tree-sitter (regex fallback parsers)"
        }
        else { Write-Fail "install failed"; exit 1 }
    }
    else { Write-Ok "crystal installed" }
}
finally { Pop-Location }

# --- 4. PATH ---------------------------------------------------------------
Write-Step "Checking that 'crystal' is callable"
$scriptsDir = & $python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
$exe = Join-Path $scriptsDir.Trim() 'crystal.exe'
if (Test-Path $exe) {
    Write-Ok "entry point at $exe"
    if (Get-Command crystal -ErrorAction SilentlyContinue) {
        Write-Ok "'crystal' resolves on PATH"
    }
    else {
        Write-Warn "'crystal' is not on PATH yet. Add this directory:"
        Write-Host "         $($scriptsDir.Trim())" -ForegroundColor Yellow
        Write-Host '         [Environment]::SetEnvironmentVariable("Path", $env:Path + ";' + $scriptsDir.Trim() + '", "User")' -ForegroundColor DarkGray
        if ($Mode -eq 'venv') {
            Write-Host "         Or activate the venv: .\.venv\Scripts\Activate.ps1" -ForegroundColor DarkGray
        }
    }
}
else {
    Write-Warn "entry point not found; use '$python -m crystal.cli' instead"
}

# --- 5. Optional tooling ---------------------------------------------------
if ($WithSolc) {
    Write-Step "Installing solc $SolcVersion via solc-select"
    & $python -m pip install solc-select --quiet @pipTarget
    if ($LASTEXITCODE -eq 0) {
        & $python -m solc_select.__main__ install $SolcVersion 2>$null
        & $python -m solc_select.__main__ use $SolcVersion 2>$null
        if ($LASTEXITCODE -eq 0) { Write-Ok "solc $SolcVersion selected" }
        else { Write-Warn "solc-select could not select $SolcVersion; run 'solc-select install $SolcVersion' manually" }
    }
    else { Write-Warn "solc-select install failed" }
}
else {
    if (Get-Command solc -ErrorAction SilentlyContinue) { Write-Ok "solc found" }
    else { Write-Warn "solc not found (optional). Re-run with -WithSolc, or use 'crystal scan --no-solc'." }
}

foreach ($tool in @('forge', 'anvil', 'medusa', 'echidna', 'halmos', 'cargo')) {
    if (Get-Command $tool -ErrorAction SilentlyContinue) { Write-Ok "$tool found" }
    else { Write-Warn "$tool not found (optional; Crystal degrades instead of failing)" }
}
if (-not (Get-Command forge -ErrorAction SilentlyContinue)) {
    Write-Host "         Foundry on Windows: https://github.com/foundry-rs/foundry/releases" -ForegroundColor DarkGray
}

# --- 6. Verify -------------------------------------------------------------
Write-Step "Running 'crystal doctor'"
& $python -m crystal.cli doctor
$doctor = $LASTEXITCODE

Write-Host ""
if ($doctor -eq 0) {
    Write-Host "Crystal is ready." -ForegroundColor Green
    Write-Host "  crystal scan .\examples --no-solc"
    Write-Host "  crystal scan .\target --format sarif -o crystal.sarif"
}
else {
    Write-Host "Crystal installed, but 'doctor' reported a blocking issue above." -ForegroundColor Yellow
}
exit $doctor
