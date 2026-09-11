$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdministrator = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdministrator) {
    Write-Host "Requesting Windows administrator permission..." -ForegroundColor Yellow
    try {
        $arguments = @(
            '-NoLogo'
            '-NoProfile'
            '-ExecutionPolicy'
            'Bypass'
            '-File'
            ('"' + $PSCommandPath + '"')
        )
        Start-Process -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
            -ArgumentList $arguments -Verb RunAs -WindowStyle Normal
    }
    catch {
        Write-Host "Unable to request administrator permission: $($_.Exception.Message)" -ForegroundColor Red
        Read-Host "Press Enter to close this window"
    }
    exit
}

$logPath = Join-Path $PSScriptRoot "install-docker-local.log"
Start-Transcript -Path $logPath -Append

try {
    Write-Host "[1/4] Enabling Windows Subsystem for Linux..." -ForegroundColor Cyan
    & dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
    if ($LASTEXITCODE -notin @(0, 3010)) {
        throw "Failed to enable WSL. DISM exit code: $LASTEXITCODE"
    }

    Write-Host "[2/4] Enabling Virtual Machine Platform..." -ForegroundColor Cyan
    & dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
    if ($LASTEXITCODE -notin @(0, 3010)) {
        throw "Failed to enable Virtual Machine Platform. DISM exit code: $LASTEXITCODE"
    }

    Write-Host "[3/4] Updating WSL..." -ForegroundColor Cyan
    & wsl.exe --update --web-download
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "WSL update did not complete. It can be retried after restart."
    }

    Write-Host "[4/4] Installing Docker Desktop..." -ForegroundColor Cyan
    $winget = (Get-Command winget.exe -ErrorAction Stop).Source
    & $winget install --exact --id Docker.DockerDesktop --source winget --accept-package-agreements --accept-source-agreements --silent
    if ($LASTEXITCODE -notin @(0, -1978335189)) {
        throw "Docker Desktop installation failed. winget exit code: $LASTEXITCODE"
    }

    Write-Host ""
    Write-Host "Installation stage completed." -ForegroundColor Green
    Write-Host "Restart Windows once, then return to Codex and report that restart is complete." -ForegroundColor Yellow
}
catch {
    Write-Host "Installation failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Log: $logPath" -ForegroundColor Yellow
}
finally {
    Stop-Transcript
    Read-Host "Press Enter to close this window"
}
