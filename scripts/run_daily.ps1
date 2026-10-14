# Run the TradingAgents watchlist once, unattended (for Windows Task Scheduler).
# Settings come from .env: TRADINGAGENTS_WATCHLIST, provider/models, language,
# round counts, and the report answers. Output goes to logs\daily_YYYY-MM-DD.log.
$ErrorActionPreference = "Continue"
$Project = Split-Path -Parent $PSScriptRoot
Set-Location $Project
$Conda = Join-Path $env:USERPROFILE "miniconda3\shell\condabin\conda-hook.ps1"
. $Conda
conda activate tradingagents
$env:PYTHONIOENCODING = "utf-8"
New-Item -ItemType Directory -Force -Path (Join-Path $Project "logs") | Out-Null
$Log = Join-Path $Project ("logs\daily_" + (Get-Date -Format "yyyy-MM-dd") + ".log")
tradingagents daily *>&1 | Tee-Object -FilePath $Log -Append
tradingagents scoreboard *>&1 | Tee-Object -FilePath $Log -Append
