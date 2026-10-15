# Keep the TradingAgents web service running (started at sign-in by the launcher that
# `tradingagents server service install` puts in the Startup folder). If the service
# stops for any reason, it is started again after 15 seconds. Output goes to
# logs\server_YYYY-MM-DD.log, in UTF-8.
param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8000
)
$ErrorActionPreference = "Continue"
$Project = Split-Path -Parent $PSScriptRoot
Set-Location $Project
$Conda = Join-Path $env:USERPROFILE "miniconda3\shell\condabin\conda-hook.ps1"
. $Conda
conda activate tradingagents
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
New-Item -ItemType Directory -Force -Path (Join-Path $Project "logs") | Out-Null
while ($true) {
    $Log = Join-Path $Project ("logs\server_" + (Get-Date -Format "yyyy-MM-dd") + ".log")
    Add-Content -Path $Log -Encoding UTF8 -Value ("=== start " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + " on ${BindHost}:${Port} ===")
    # cmd does the redirection, so the log gets the service's own UTF-8 bytes (PowerShell 5
    # would re-encode them as UTF-16 and flag every line on stderr as an error).
    cmd /c "tradingagents server run --host $BindHost --port $Port >> `"$Log`" 2>&1"
    Add-Content -Path $Log -Encoding UTF8 -Value ("=== stopped " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + " (exit $LASTEXITCODE); restarting in 15 s ===")
    Start-Sleep -Seconds 15
}
