param(
    [Parameter(Mandatory=$true)][string]$OutputRoot,
    [Parameter(Mandatory=$true)][int]$OrchestratorPid,
    [double]$StopAfterHours = 10.0,
    [int]$IntervalSeconds = 900
)

$ErrorActionPreference = "Continue"
$deadline = (Get-Date).AddHours($StopAfterHours)
$monitorLog = Join-Path $OutputRoot "monitor_status.csv"
$monitorReport = Join-Path $OutputRoot "monitor_status.md"

function Write-MonitorStatus {
    param([string]$State, [string]$Message)

    $statusPath = Join-Path $OutputRoot "status.csv"
    $rows = @()
    if (Test-Path $statusPath) {
        try { $rows = Import-Csv $statusPath } catch { $rows = @() }
    }
    $latest = @($rows | Select-Object -Last 8)
    $completed = @($rows | Where-Object { $_.stage -eq "stage3_registry_label" -and $_.status -eq "ok" }).Count
    $failed = @($rows | Where-Object { $_.status -ne "ok" }).Count
    $running = [bool](Get-Process -Id $OrchestratorPid -ErrorAction SilentlyContinue)
    $now = Get-Date
    $nowText = $now.ToString("o")
    $deadlineText = $deadline.ToString("o")

    $line = [pscustomobject]@{
        timestamp = $nowText
        orchestrator_pid = $OrchestratorPid
        running = $running
        state = $State
        deadline = $deadlineText
        status_rows = @($rows).Count
        completed_stage3 = $completed
        failed_or_skipped_rows = $failed
        message = $Message
    }
    $exists = Test-Path $monitorLog
    $line | Export-Csv $monitorLog -Append -NoTypeInformation -Encoding UTF8
    if (-not $exists) {
        # Export-Csv already created the header; this branch documents intent.
    }

    $latestLines = $latest | ForEach-Object {
        "| $($_.timestamp) | $($_.wave) | $($_.sample_size) | $($_.language) | $($_.stage) | $($_.status) |"
    }
    $body = New-Object System.Collections.Generic.List[string]
    $body.Add("# Overnight Experiment Monitor")
    $body.Add("")
    $body.Add("- State: ``$State``")
    $body.Add("- Message: $Message")
    $body.Add("- Generated: $nowText")
    $body.Add("- Deadline: $deadlineText")
    $body.Add("- Orchestrator PID: ``$OrchestratorPid``")
    $body.Add("- Orchestrator running: ``$running``")
    $body.Add("- Status rows: $(@($rows).Count)")
    $body.Add("- Completed stage3 rows: $completed")
    $body.Add("- Failed/skipped/non-ok rows: $failed")
    $body.Add("")
    $body.Add("## Latest Status Rows")
    $body.Add("")
    $body.Add("| Timestamp | Wave | Sample | Language | Stage | Status |")
    $body.Add("|---|---:|---:|---|---|---|")
    foreach ($line in $latestLines) {
        $body.Add($line)
    }
    $body -join "`n" | Set-Content -Path $monitorReport -Encoding UTF8

    try {
        & "C:\ProgramData\Anaconda3\python.exe" "scripts/hallucination/analyze_overnight_progress.py" "--output-root" $OutputRoot |
            Out-File -FilePath (Join-Path $OutputRoot "progress_analysis_hook.log") -Append -Encoding UTF8
        & "C:\ProgramData\Anaconda3\python.exe" "scripts/hallucination/audit_wave_results.py" "--output-root" $OutputRoot |
            Out-File -FilePath (Join-Path $OutputRoot "wave_audit_hook.log") -Append -Encoding UTF8
    } catch {
        "analysis_hook_error=$($_.Exception.Message)" |
            Out-File -FilePath (Join-Path $OutputRoot "progress_analysis_hook.log") -Append -Encoding UTF8
    }
}

Write-MonitorStatus -State "started" -Message "watchdog started"

while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds $IntervalSeconds
    if (-not (Get-Process -Id $OrchestratorPid -ErrorAction SilentlyContinue)) {
        Write-MonitorStatus -State "orchestrator_exited" -Message "orchestrator process exited before hard deadline"
        try {
            & "C:\ProgramData\Anaconda3\python.exe" "scripts/hallucination/finalize_overnight_results.py" "--output-root" $OutputRoot |
                Out-File -FilePath (Join-Path $OutputRoot "final_audit_hook.log") -Append -Encoding UTF8
        } catch {
            "final_audit_hook_error=$($_.Exception.Message)" |
                Out-File -FilePath (Join-Path $OutputRoot "final_audit_hook.log") -Append -Encoding UTF8
        }
        exit 0
    }
    Write-MonitorStatus -State "running" -Message "periodic check"
}

Write-MonitorStatus -State "deadline_reached" -Message "hard stop deadline reached"

$escapedRoot = [regex]::Escape($OutputRoot)
$related = Get-CimInstance Win32_Process -Filter "name = 'python.exe'" |
    Where-Object { $_.ProcessId -eq $OrchestratorPid -or $_.CommandLine -match $escapedRoot }
foreach ($proc in $related) {
    try { Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop } catch {}
}

& "C:\ProgramData\Anaconda3\python.exe" "scripts/hallucination/run_refined_overnight.py" `
    "--output-root" $OutputRoot `
    "--time-budget-hours" "0" `
    "--sample-sizes" "30" "50" "100" "200" | Out-File -FilePath (Join-Path $OutputRoot "monitor_finalize.log") -Encoding UTF8

& "C:\ProgramData\Anaconda3\python.exe" "scripts/hallucination/finalize_overnight_results.py" `
    "--output-root" $OutputRoot | Out-File -FilePath (Join-Path $OutputRoot "final_audit_hook.log") -Append -Encoding UTF8

Write-MonitorStatus -State "stopped_and_finalized" -Message "related Python processes stopped and final report generation requested"
