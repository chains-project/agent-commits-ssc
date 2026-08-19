<#
[IN]: four-agent population CSV and non-Claude optimized raw/cleaned rows.
[OUT]: data_products/agent_commit_population_merged_1y_v1/merged_agent_commit_population.csv and summary/manifest files.
[POS]: Produces the merged annual commit population consumed by repo metadata, language joins, diff fetching, and RQ3 pilots.
[SYNC]: If merge precedence, evidence columns, or output names change, update scripts/CLAUDE.md and scripts/OUTPUTS.md.
#>param(
    [string]$FourAgentPopulation = "",
    [string]$ThreeAgentRaw = "",
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Stop"

function Resolve-InputPath {
    param([string]$Path, [string]$DefaultRelative)
    if ($Path) { return (Resolve-Path -LiteralPath $Path).Path }
    return (Resolve-Path -LiteralPath (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) $DefaultRelative)).Path
}

function Add-Token {
    param([string]$Current, [string]$Token)
    if (-not $Token) { return $Current }
    if (-not $Current) { return $Token }
    $needle = ";$Token;"
    $haystack = ";$Current;"
    if ($haystack.Contains($needle)) { return $Current }
    return "$Current;$Token"
}

function Csv-Escape {
    param([object]$Value)
    if ($null -eq $Value) { return '""' }
    $text = [string]$Value
    $text = $text -replace '"', '""'
    return '"' + $text + '"'
}

function Write-CsvRow {
    param(
        [System.IO.StreamWriter]$Writer,
        [object[]]$Values
    )
    $escaped = foreach ($value in $Values) { Csv-Escape $value }
    $Writer.WriteLine(($escaped -join ","))
}

function Is-ValidTier {
    param([string]$Tier)
    return $Tier -in @("main", "audit", "monthly_audit")
}

function New-EntryFromFourAgent {
    param([object]$Row)
    return [ordered]@{
        agent = [string]$Row.agent
        repo = [string]$Row.repo
        sha = [string]$Row.sha
        repo_sha = [string]$Row.repo_sha
        evidence_scope = [string]$Row.evidence_scope
        evidence_type = [string]$Row.evidence_type
        evidence_channels = [string]$Row.evidence_channels
        evidence_tiers = [string]$Row.evidence_tiers
        evidence_modes = [string]$Row.evidence_modes
        sample_sources = "four_agent_corrected_v1_10min"
        present_in_four_agent_10min = "true"
        present_in_three_agent_4h = "false"
        author_name = [string]$Row.author_name
        author_email = [string]$Row.author_email
        author_date = [string]$Row.author_date
        html_url = [string]$Row.html_url
        message_first_line = [string]$Row.message_first_line
    }
}

function New-EntryFromThreeAgent {
    param([object]$Row)
    return [ordered]@{
        agent = [string]$Row.agent
        repo = [string]$Row.repo
        sha = [string]$Row.sha
        repo_sha = [string]$Row.repo_sha
        evidence_scope = "strict_agent_commit"
        evidence_type = "commit_signature"
        evidence_channels = [string]$Row.channel
        evidence_tiers = [string]$Row.tier
        evidence_modes = [string]$Row.mode
        sample_sources = "three_agent_nonclaude_optimized_4h"
        present_in_four_agent_10min = "false"
        present_in_three_agent_4h = "true"
        author_name = [string]$Row.author_name
        author_email = [string]$Row.author_email
        author_date = [string]$Row.author_date
        html_url = [string]$Row.html_url
        message_first_line = [string]$Row.message_first_line
    }
}

if (-not $OutputDir) {
    $OutputDir = Join-Path (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) "data_products") "agent_commit_population_merged_1y_v1"
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$fourPath = Resolve-InputPath -Path $FourAgentPopulation -DefaultRelative "data_products\agent_commit_population_corrected_v1\agent_commit_population.csv"
$threeRawPath = Resolve-InputPath -Path $ThreeAgentRaw -DefaultRelative "scripts\pilot\nonclaude_optimized_2026-06-12T12-32-47Z\sha_channels_raw.csv"

$entries = @{}
$globalRepoSha = @{}
$fourRows = 0
$threeRowsValid = 0
$threeRowsInvalid = 0
$validThreeAgents = @{ codex = $true; copilot = $true; cursor = $true }

Write-Output "read_four_agent_population=$fourPath"
Import-Csv -LiteralPath $fourPath | ForEach-Object {
    $agent = [string]$_.agent
    $repoSha = [string]$_.repo_sha
    if (-not $agent -or -not $repoSha) { return }
    $key = "$agent|$repoSha"
    if (-not $entries.ContainsKey($key)) {
        $entries[$key] = New-EntryFromFourAgent -Row $_
    } else {
        $entry = $entries[$key]
        $entry.sample_sources = Add-Token $entry.sample_sources "four_agent_corrected_v1_10min"
        $entry.present_in_four_agent_10min = "true"
        $entry.evidence_channels = Add-Token $entry.evidence_channels ([string]$_.evidence_channels)
        $entry.evidence_tiers = Add-Token $entry.evidence_tiers ([string]$_.evidence_tiers)
        $entry.evidence_modes = Add-Token $entry.evidence_modes ([string]$_.evidence_modes)
    }
    $globalRepoSha[$repoSha] = $true
    $fourRows++
}

Write-Output "scan_three_agent_raw=$threeRawPath"
Import-Csv -LiteralPath $threeRawPath | ForEach-Object {
    $agent = [string]$_.agent
    $repoSha = [string]$_.repo_sha
    $tier = [string]$_.tier
    if (-not $repoSha -or -not $validThreeAgents.ContainsKey($agent) -or -not (Is-ValidTier $tier)) {
        $threeRowsInvalid++
        return
    }
    $threeRowsValid++
    $key = "$agent|$repoSha"
    if (-not $entries.ContainsKey($key)) {
        $entries[$key] = New-EntryFromThreeAgent -Row $_
    } else {
        $entry = $entries[$key]
        $entry.sample_sources = Add-Token $entry.sample_sources "three_agent_nonclaude_optimized_4h"
        $entry.present_in_three_agent_4h = "true"
        $entry.evidence_channels = Add-Token $entry.evidence_channels ([string]$_.channel)
        $entry.evidence_tiers = Add-Token $entry.evidence_tiers $tier
        $entry.evidence_modes = Add-Token $entry.evidence_modes ([string]$_.mode)
        if (-not $entry.author_date -and $_.author_date) {
            $entry.author_name = [string]$_.author_name
            $entry.author_email = [string]$_.author_email
            $entry.author_date = [string]$_.author_date
            $entry.html_url = [string]$_.html_url
            $entry.message_first_line = [string]$_.message_first_line
        }
    }
    $globalRepoSha[$repoSha] = $true
}

$mergedPath = Join-Path $OutputDir "merged_agent_commit_population.csv"
$summaryPath = Join-Path $OutputDir "merged_summary.csv"
$agentSummaryPath = Join-Path $OutputDir "merged_agent_summary.csv"
$sourceSummaryPath = Join-Path $OutputDir "merged_source_summary.csv"

Write-Output "write_merged=$mergedPath"
$encoding = [Text.UTF8Encoding]::new($false)
$writer = [System.IO.StreamWriter]::new($mergedPath, $false, $encoding)
try {
    Write-CsvRow -Writer $writer -Values @(
        "agent", "repo", "sha", "repo_sha",
        "evidence_scope", "evidence_type", "evidence_channels", "evidence_tiers", "evidence_modes",
        "sample_sources", "present_in_four_agent_10min", "present_in_three_agent_4h",
        "author_name", "author_email", "author_date", "html_url", "message_first_line"
    )
    foreach ($key in $entries.Keys) {
        $e = $entries[$key]
        Write-CsvRow -Writer $writer -Values @(
            $e.agent, $e.repo, $e.sha, $e.repo_sha,
            $e.evidence_scope, $e.evidence_type, $e.evidence_channels, $e.evidence_tiers, $e.evidence_modes,
            $e.sample_sources, $e.present_in_four_agent_10min, $e.present_in_three_agent_4h,
            $e.author_name, $e.author_email, $e.author_date, $e.html_url, $e.message_first_line
        )
    }
} finally {
    $writer.Dispose()
}

$agentRows = New-Object System.Collections.Generic.List[object]
$sourceRows = New-Object System.Collections.Generic.List[object]
$agentStats = @{}
$sourceStats = @{}
foreach ($key in $entries.Keys) {
    $e = $entries[$key]
    if (-not $agentStats.ContainsKey($e.agent)) {
        $agentStats[$e.agent] = @{ rows = 0; repo_sha = @{}; four = 0; three = 0; both = 0 }
    }
    $agentStats[$e.agent].rows++
    $agentStats[$e.agent].repo_sha[$e.repo_sha] = $true
    if ($e.present_in_four_agent_10min -eq "true") { $agentStats[$e.agent].four++ }
    if ($e.present_in_three_agent_4h -eq "true") { $agentStats[$e.agent].three++ }
    if ($e.present_in_four_agent_10min -eq "true" -and $e.present_in_three_agent_4h -eq "true") { $agentStats[$e.agent].both++ }

    if ($e.present_in_four_agent_10min -eq "true" -and $e.present_in_three_agent_4h -eq "true") {
        $sourceClass = "both"
    } elseif ($e.present_in_four_agent_10min -eq "true") {
        $sourceClass = "four_agent_only"
    } else {
        $sourceClass = "three_agent_only"
    }
    if (-not $sourceStats.ContainsKey($sourceClass)) {
        $sourceStats[$sourceClass] = @{ rows = 0; repo_sha = @{} }
    }
    $sourceStats[$sourceClass].rows++
    $sourceStats[$sourceClass].repo_sha[$e.repo_sha] = $true
}

foreach ($agent in ($agentStats.Keys | Sort-Object)) {
    $s = $agentStats[$agent]
    $agentRows.Add([pscustomobject]@{
        agent = $agent
        merged_agent_repo_sha_rows = $s.rows
        unique_repo_sha = $s.repo_sha.Count
        present_in_four_agent_10min = $s.four
        present_in_three_agent_4h = $s.three
        present_in_both_sources = $s.both
    }) | Out-Null
}
$agentRows | Export-Csv -LiteralPath $agentSummaryPath -NoTypeInformation -Encoding UTF8

foreach ($sourceClass in ($sourceStats.Keys | Sort-Object)) {
    $s = $sourceStats[$sourceClass]
    $sourceRows.Add([pscustomobject]@{
        source_class = $sourceClass
        merged_agent_repo_sha_rows = $s.rows
        unique_repo_sha = $s.repo_sha.Count
    }) | Out-Null
}
$sourceRows | Export-Csv -LiteralPath $sourceSummaryPath -NoTypeInformation -Encoding UTF8

$summary = [pscustomobject]@{
    merged_version = "agent_commit_population_merged_1y_v1"
    grain = "one row per agent plus repo_sha attribution"
    four_agent_population = $fourPath
    three_agent_raw = $threeRawPath
    four_agent_rows_read = $fourRows
    three_agent_valid_raw_rows_scanned = $threeRowsValid
    three_agent_invalid_raw_rows_excluded = $threeRowsInvalid
    merged_agent_repo_sha_rows = $entries.Count
    merged_unique_repo_sha = $globalRepoSha.Count
    created_utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}
$summary | Export-Csv -LiteralPath $summaryPath -NoTypeInformation -Encoding UTF8
$summary | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $OutputDir "manifest.json") -Encoding UTF8

Write-Output "merged_rows=$($entries.Count) unique_repo_sha=$($globalRepoSha.Count)"
Write-Output "wrote=$mergedPath"
Write-Output "wrote=$summaryPath"
Write-Output "wrote=$agentSummaryPath"
Write-Output "wrote=$sourceSummaryPath"
