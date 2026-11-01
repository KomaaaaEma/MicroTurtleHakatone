param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

try {
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($pythonCommand) {
            & $pythonCommand.Source -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11 or newer is required'"
            if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11 or newer and enable Add Python to PATH.' }
            & $pythonCommand.Source -m venv .venv
        } else {
            $pyCommand = Get-Command py -ErrorAction SilentlyContinue
            if (-not $pyCommand) { throw 'Python not found. Install Python 3.11 or newer from python.org.' }
            & $pyCommand.Source -3 -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11 or newer is required'"
            if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11 or newer.' }
            & $pyCommand.Source -3 -m venv .venv
        }
        if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv.' }
    }

    $studioPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    # Use .NET directly: Windows PowerShell can inherit a PSModulePath that
    # does not expose the module containing Get-FileHash.
    $studioHasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        $requirementsHash = [BitConverter]::ToString($studioHasher.ComputeHash(
            [IO.File]::ReadAllBytes((Join-Path $PSScriptRoot 'requirements.txt')))).Replace('-', '')
    } finally {
        $studioHasher.Dispose()
    }
    $stampPath = Join-Path $PSScriptRoot '.venv\requirements.sha256'
    $previousHash = if (Test-Path -LiteralPath $stampPath) { (Get-Content -LiteralPath $stampPath -Raw).Trim() } else { '' }
    if ($requirementsHash -ne $previousHash) {
        & $studioPython -m pip install -r requirements.txt
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check Internet access and retry.' }
        Set-Content -LiteralPath $stampPath -Value $requirementsHash -Encoding ascii
    }

    Write-Host ''
    Write-Host "MicroTurtles: http://127.0.0.1:$Port" -ForegroundColor Cyan
    Write-Host "OpenAPI:     http://127.0.0.1:$Port/api/docs"
    Write-Host 'Press Ctrl+C to stop. Data is saved in data/.'
    Write-Host ''
    & $studioPython -m uvicorn app.main:app --host 127.0.0.1 --port $Port
    exit $LASTEXITCODE
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
