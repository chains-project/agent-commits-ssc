<#
[IN]: corrected four-agent annual sample and non-Claude optimized sample directories.
[OUT]: cleaned non-Claude CSV/manifest products and four-agent agent_commit_population.csv under data_products.
[POS]: Bridge from raw pilot collection directories to stable commit population data products.
[SYNC]: If cleaning rules, output filenames, or population columns change, update scripts/CLAUDE.md and scripts/OUTPUTS.md.
#>param(
    [string]$AnnualCorrectedDir = "",
    [string]$NonClaudeDir = "",
    [string]$OutputRoot = "",
    [switch]$SkipNonClaudeClean,
    [switch]$SkipPopulation
)

$ErrorActionPreference = "Stop"

function Resolve-DefaultPath {
    param([string]$Path, [string]$DefaultRelative)
    if ($Path) { return (Resolve-Path -LiteralPath $Path).Path }
    return (Resolve-Path -LiteralPath (Join-Path (Split-Path -Parent $PSScriptRoot) $DefaultRelative)).Path
}

function New-Set {
    return @{}
}

function Add-ToSetMap {
    param(
        [hashtable]$Map,
        [string]$Key,
        [string]$Value
    )
    if (-not $Map.ContainsKey($Key)) {
        $Map[$Key] = New-Set
    }
    $Map[$Key][$Value] = $true
}

function Inc-Map {
    param(
        [hashtable]$Map,
        [string]$Key,
        [int]$By = 1
    )
    if (-not $Map.ContainsKey($Key)) {
        $Map[$Key] = 0
    }
    $Map[$Key] += $By
}

function Is-ValidTier {
    param([string]$Tier)
    return $Tier -in @("main", "audit", "monthly_audit")
}

function Export-Rows {
    param(
        [object[]]$Rows,
        [string]$Path
    )
    $dir = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $Rows | Export-Csv -LiteralPath $Path -NoTypeInformation -Encoding UTF8
}

function Join-Keys {
    param([hashtable]$Set)
    return (($Set.Keys | Sort-Object -Unique) -join ";")
}

function Build-NonClaudeCleanedV1 {
    param(
        [string]$SourceDir,
        [string]$OutDir
    )

    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
    $rawPath = Join-Path $SourceDir "sha_channels_raw.csv"
    $segmentsPath = Join-Path $SourceDir "query_segments.csv"
    $windowsPath = Join-Path $SourceDir "sampled_windows.csv"
    if (-not (Test-Path -LiteralPath $rawPath)) { throw "Missing $rawPath" }

    $validAgents = @{ codex = $true; copilot = $true; cursor = $true }
    $globalSet = New-Set
    $agentSets = @{}
    $agentRaw = @{}
    $channelSets = @{}
    $channelRaw = @{}
    $tierSets = @{}
    $tierRaw = @{}
    $invalidRows = New-Object System.Collections.Generic.List[object]
    $validRaw = 0
    $invalidRaw = 0

    Write-Output "scan_nonclaude_raw=$rawPath"
    Import-Csv -LiteralPath $rawPath | ForEach-Object {
        $tier = [string]$_.tier
        $agent = [string]$_.agent
        $repoSha = [string]$_.repo_sha
        if ((-not (Is-ValidTier $tier)) -or (-not $validAgents.ContainsKey($agent)) -or (-not $repoSha)) {
            $invalidRaw++
            if ($invalidRows.Count -lt 100) {
                $invalidRows.Add([pscustomobject]@{
                    tier = $tier
                    agent = $agent
                    channel = [string]$_.channel
                    mode = [string]$_.mode
                    repo = [string]$_.repo
                    sha = [string]$_.sha
                    repo_sha = $repoSha
                    reason = "invalid_tier_or_agent_or_repo_sha"
                }) | Out-Null
            }
            return
        }

        $validRaw++
        $globalSet[$repoSha] = $true

        Inc-Map -Map $agentRaw -Key $agent
        Add-ToSetMap -Map $agentSets -Key $agent -Value $repoSha

        $channelKey = "$tier|$agent|$($_.channel)|$($_.mode)"
        Inc-Map -Map $channelRaw -Key $channelKey
        Add-ToSetMap -Map $channelSets -Key $channelKey -Value $repoSha

        Inc-Map -Map $tierRaw -Key $tier
        Add-ToSetMap -Map $tierSets -Key $tier -Value $repoSha
    }

    $channelRows = foreach ($key in ($channelSets.Keys | Sort-Object)) {
        $parts = $key -split "\|", 4
        [pscustomobject]@{
            tier = $parts[0]
            agent = $parts[1]
            channel = $parts[2]
            mode = $parts[3]
            raw_rows_before_dedup = $channelRaw[$key]
            unique_repo_sha_after_dedup = $channelSets[$key].Count
        }
    }
    Export-Rows -Rows @($channelRows) -Path (Join-Path $OutDir "channel_counts_clean.csv")

    $agentRows = foreach ($agent in ($agentSets.Keys | Sort-Object)) {
        $channelUniqueSum = 0
        foreach ($row in @($channelRows | Where-Object { $_.agent -eq $agent })) {
            $channelUniqueSum += [int]$row.unique_repo_sha_after_dedup
        }
        $ratio = 0.0
        if ($channelUniqueSum -gt 0) {
            $ratio = [math]::Round($agentSets[$agent].Count / $channelUniqueSum, 6)
        }
        [pscustomobject]@{
            agent = $agent
            raw_rows_before_dedup = $agentRaw[$agent]
            sum_channel_unique_before_cross_channel_dedup = $channelUniqueSum
            unique_repo_sha_after_dedup = $agentSets[$agent].Count
            cross_channel_dedup_ratio = $ratio
        }
    }
    Export-Rows -Rows @($agentRows) -Path (Join-Path $OutDir "agent_counts_clean.csv")

    $filterRows = New-Object System.Collections.Generic.List[object]
    $filterRows.Add([pscustomobject]@{
        scope = "all_valid"
        raw_rows_before_dedup = $validRaw
        unique_repo_sha_after_dedup = $globalSet.Count
    }) | Out-Null
    $filterRows.Add([pscustomobject]@{
        scope = "invalid_excluded"
        raw_rows_before_dedup = $invalidRaw
        unique_repo_sha_after_dedup = ""
    }) | Out-Null
    foreach ($row in $agentRows) {
        $filterRows.Add([pscustomobject]@{
            scope = "agent:$($row.agent)"
            raw_rows_before_dedup = $row.raw_rows_before_dedup
            unique_repo_sha_after_dedup = $row.unique_repo_sha_after_dedup
        }) | Out-Null
    }
    foreach ($tier in ($tierSets.Keys | Sort-Object)) {
        $filterRows.Add([pscustomobject]@{
            scope = "tier:$tier"
            raw_rows_before_dedup = $tierRaw[$tier]
            unique_repo_sha_after_dedup = $tierSets[$tier].Count
        }) | Out-Null
    }
    Export-Rows -Rows @($filterRows.ToArray()) -Path (Join-Path $OutDir "filter_counts_clean.csv")
    Export-Rows -Rows @($invalidRows.ToArray()) -Path (Join-Path $OutDir "invalid_rows_sample.csv")

    if (Test-Path -LiteralPath $windowsPath) {
        $windowRows = Import-Csv -LiteralPath $windowsPath | ForEach-Object {
            $start = [string]$_.window_start_utc
            $sampleDate = [string]$_.sample_date_utc
            if (-not $sampleDate -and $start.Length -ge 10) {
                $sampleDate = $start.Substring(0, 10)
            }
            [pscustomobject]@{
                sample_date_utc = $sampleDate
                window_start_utc = $start
                window_end_utc = [string]$_.window_end_utc
                seed = [string]$_.seed
                bucket = [string]$_.bucket
            }
        }
        Export-Rows -Rows @($windowRows) -Path (Join-Path $OutDir "sampled_windows_clean.csv")
    }

    if (Test-Path -LiteralPath $segmentsPath) {
        $segments = @(Import-Csv -LiteralPath $segmentsPath)
        $statusRows = $segments | Group-Object status | Sort-Object Name | ForEach-Object {
            [pscustomobject]@{ status = $_.Name; count = $_.Count }
        }
        Export-Rows -Rows @($statusRows) -Path (Join-Path $OutDir "query_status_counts_clean.csv")
        $capRows = @($segments | Where-Object { $_.status -eq "cap_bound_terminal" })
        Export-Rows -Rows $capRows -Path (Join-Path $OutDir "cap_bound_terminal_segments.csv")
    }

    $manifest = [pscustomobject]@{
        cleaned_version = "nonclaude_optimized_cleaned_v1"
        source_dir = $SourceDir
        raw_file = $rawPath
        output_dir = $OutDir
        valid_raw_rows = $validRaw
        invalid_excluded_rows = $invalidRaw
        unique_repo_sha = $globalSet.Count
        created_utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        note = "Raw data are preserved in source_dir; this directory contains cleaned summaries only."
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $OutDir "manifest.json") -Encoding UTF8
    Write-Output "cleaned_nonclaude_out=$OutDir valid_raw=$validRaw invalid=$invalidRaw unique=$($globalSet.Count)"
}

function Build-AgentCommitPopulation {
    param(
        [string]$SourceDir,
        [string]$OutDir
    )

    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
    $rawPath = Join-Path $SourceDir "sha_channels_raw.csv"
    if (-not (Test-Path -LiteralPath $rawPath)) { throw "Missing $rawPath" }

    $validAgents = @{ claude = $true; codex = $true; copilot = $true; cursor = $true }
    $entries = @{}
    $rawValid = 0
    $invalid = 0

    Write-Output "scan_annual_raw=$rawPath"
    Import-Csv -LiteralPath $rawPath | ForEach-Object {
        $tier = [string]$_.tier
        $agent = [string]$_.agent
        $repoSha = [string]$_.repo_sha
        if ((-not (Is-ValidTier $tier)) -or (-not $validAgents.ContainsKey($agent)) -or (-not $repoSha)) {
            $invalid++
            return
        }
        $rawValid++
        $key = "$agent|$repoSha"
        if (-not $entries.ContainsKey($key)) {
            $entries[$key] = [ordered]@{
                agent = $agent
                repo = [string]$_.repo
                sha = [string]$_.sha
                repo_sha = $repoSha
                evidence_scope = "strict_agent_commit"
                evidence_type = "commit_signature"
                sample_source = "annual_corrected_v1_10min"
                author_name = [string]$_.author_name
                author_email = [string]$_.author_email
                author_date = [string]$_.author_date
                html_url = [string]$_.html_url
                message_first_line = [string]$_.message_first_line
                channels = @{}
                tiers = @{}
                modes = @{}
            }
        }
        $entry = $entries[$key]
        $entry.channels[[string]$_.channel] = $true
        $entry.tiers[$tier] = $true
        $entry.modes[[string]$_.mode] = $true
    }

    $populationRows = foreach ($key in ($entries.Keys | Sort-Object)) {
        $entry = $entries[$key]
        [pscustomobject]@{
            agent = $entry.agent
            repo = $entry.repo
            sha = $entry.sha
            repo_sha = $entry.repo_sha
            evidence_scope = $entry.evidence_scope
            evidence_type = $entry.evidence_type
            evidence_channels = Join-Keys $entry.channels
            evidence_tiers = Join-Keys $entry.tiers
            evidence_modes = Join-Keys $entry.modes
            sample_source = $entry.sample_source
            author_name = $entry.author_name
            author_email = $entry.author_email
            author_date = $entry.author_date
            html_url = $entry.html_url
            message_first_line = $entry.message_first_line
        }
    }
    $populationPath = Join-Path $OutDir "agent_commit_population.csv"
    Export-Rows -Rows @($populationRows) -Path $populationPath

    $globalSet = New-Set
    $summaryRows = New-Object System.Collections.Generic.List[object]
    foreach ($row in $populationRows) {
        $globalSet[$row.repo_sha] = $true
    }
    $summaryRows.Add([pscustomobject]@{
        scope = "all_agent_attributions"
        rows = $entries.Count
        unique_repo_sha = $globalSet.Count
        raw_valid_rows_scanned = $rawValid
        invalid_rows_excluded = $invalid
    }) | Out-Null
    foreach ($group in ($populationRows | Group-Object agent | Sort-Object Name)) {
        $set = New-Set
        foreach ($row in $group.Group) { $set[$row.repo_sha] = $true }
        $summaryRows.Add([pscustomobject]@{
            scope = "agent:$($group.Name)"
            rows = $group.Count
            unique_repo_sha = $set.Count
            raw_valid_rows_scanned = ""
            invalid_rows_excluded = ""
        }) | Out-Null
    }
    Export-Rows -Rows @($summaryRows.ToArray()) -Path (Join-Path $OutDir "agent_commit_population_summary.csv")

    $manifest = [pscustomobject]@{
        population_version = "agent_commit_population_corrected_v1"
        source_dir = $SourceDir
        raw_file = $rawPath
        output_dir = $OutDir
        row_grain = "one row per agent plus repo_sha attribution"
        evidence_scope = "strict_agent_commit"
        rows = $entries.Count
        unique_repo_sha = $globalSet.Count
        raw_valid_rows_scanned = $rawValid
        invalid_rows_excluded = $invalid
        created_utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $OutDir "manifest.json") -Encoding UTF8
    Write-Output "population_out=$OutDir rows=$($entries.Count) unique_repo_sha=$($globalSet.Count)"
}

$annualDir = Resolve-DefaultPath -Path $AnnualCorrectedDir -DefaultRelative "pilot\annual_sample_2026-06-10T11-06-53Z_corrected_v1"
$nonClaudeSourceDir = Resolve-DefaultPath -Path $NonClaudeDir -DefaultRelative "pilot\nonclaude_optimized_2026-06-12T12-32-47Z"
if ($OutputRoot) {
    $outRoot = $OutputRoot
} else {
    $outRoot = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) "data_products"
}
New-Item -ItemType Directory -Force -Path $outRoot | Out-Null

$cleanOut = Join-Path $outRoot "nonclaude_optimized_cleaned_v1"
$populationOut = Join-Path $outRoot "agent_commit_population_corrected_v1"

if (-not $SkipNonClaudeClean) {
    Build-NonClaudeCleanedV1 -SourceDir $nonClaudeSourceDir -OutDir $cleanOut
} else {
    Write-Output "skip_nonclaude_clean=$cleanOut"
}

if (-not $SkipPopulation) {
    Build-AgentCommitPopulation -SourceDir $annualDir -OutDir $populationOut
} else {
    Write-Output "skip_population=$populationOut"
}

Write-Output "wrote=$cleanOut"
Write-Output "wrote=$populationOut"
