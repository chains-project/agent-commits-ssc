<#
[IN]: GITHUB_TOKEN, compressed channel definitions in this file, annual UTC range, sampled windows, and optional resume output directory.
[OUT]: scripts/pilot/annual_sample_<timestamp>/ with query_segments.csv, sha_channels_raw.csv, raw_commit_items/*.jsonl.gz, index CSV, schedules, and summary tables.
[POS]: Main four-agent annual sampling collector for the thesis commit population.
[SYNC]: If channels, sampling design, raw JSONL layout, or summary schemas change, update scripts/CLAUDE.md and scripts/OUTPUTS.md.
#>param(
    [string]$SinceUtc = "2025-06-01T00:00:00Z",
    [string]$UntilUtc = "2026-06-01T00:00:00Z",
    [int]$Seed = 202506,
    [int]$SampleMinutesPerDay = 10,
    [int]$PerPage = 100,
    [int]$MaxPages = 10,
    [int]$SleepSeconds = 3,
    [int]$MaxRetries = 5,
    [int]$RetryBaseSeconds = 30,
    [int]$MinWindowSeconds = 30,
    [string[]]$Agents = @("claude", "codex", "copilot", "cursor"),
    [string]$OutputDir = "",
    [switch]$Resume,
    [switch]$ListChannels,
    [int]$MaxDays = 0,
    [int]$MaxChannels = 0,
    [switch]$DisableWeeklyAudit,
    [switch]$DisableMonthlyAudit
)

$ErrorActionPreference = "Stop"

function Read-GitHubToken {
    $scriptsRoot = Split-Path -Parent $PSScriptRoot
    $envFile = Join-Path $scriptsRoot ".env"
    if (Test-Path -LiteralPath $envFile) {
        foreach ($line in Get-Content -LiteralPath $envFile) {
            if ($line -match "^\s*GITHUB_TOKEN\s*=\s*(.+?)\s*$") {
                return $Matches[1].Trim('"').Trim("'")
            }
        }
    }
    return $env:GITHUB_TOKEN
}

function Format-Utc {
    param([datetime]$Value)
    return $Value.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}

function Parse-Utc {
    param([string]$Value)
    return ([datetime]::Parse($Value, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal)).ToUniversalTime()
}

function New-Channel {
    param(
        [string]$Tier,
        [string]$Agent,
        [string]$Channel,
        [string]$Mode,
        [string]$Query,
        [string]$Reason
    )
    [pscustomobject]@{
        tier = $Tier
        agent = $Agent
        channel = $Channel
        mode = $Mode
        query = $Query
        reason = $Reason
    }
}

function Get-CompressedChannels {
    $channels = @(
        (New-Channel main claude cli_email author "author-email:noreply@anthropic.com" "main Claude Code author email"),
        (New-Channel main claude app_bot_email author "author-email:242468646+Claude@users.noreply.github.com" "Claude GitHub App bot email"),
        (New-Channel main claude author_claude_email author "author-email:claude@anthropic.com" "candidate direct Claude author email"),
        (New-Channel main claude author_claude_code_email author "author-email:claude-code@anthropic.com" "candidate direct Claude Code author email"),
        (New-Channel main claude msg_coauthored_by_claude coauthor "`"Co-authored-by: Claude`"" "model-name trailer, captures Claude Sonnet/Opus variants"),
        (New-Channel audit claude msg_cli_email coauthor "`"noreply@anthropic.com`"" "weekly audit: 99% overlap with Co-authored-by: Claude"),
        (New-Channel audit claude msg_generated_with_claude_code marker "`"Generated with Claude Code`"" "weekly audit: small marker channel"),
        (New-Channel monthly_audit claude discover_claude_code marker "`"Claude Code`"" "monthly discovery audit: broad Claude Code marker, inspect precision before promoting"),

        (New-Channel main codex cli_email author "author-email:codex@openai.com" "main Codex author email"),
        (New-Channel main codex new_bot_email author "author-email:242516109+Codex@users.noreply.github.com" "Codex GitHub App bot email"),
        (New-Channel main codex legacy_connector author "author:chatgpt-codex-connector[bot]" "legacy connector bot"),
        (New-Channel main codex msg_noreply coauthor "`"noreply@openai.com`"" "OpenAI co-author trailer email"),
        (New-Channel main codex msg_coauthored coauthor "`"Co-authored-by: Codex`"" "Codex trailer"),
        (New-Channel audit codex msg_openai_codex coauthor "`"Co-authored-by: OpenAI Codex`"" "weekly audit: low-volume specific OpenAI Codex trailer"),
        (New-Channel audit codex msg_codex_openai_email coauthor "`"codex@openai.com`"" "weekly audit: usually subset of Codex trailer"),
        (New-Channel monthly_audit codex discover_openai_codex marker "`"OpenAI Codex`"" "monthly discovery audit: broad OpenAI Codex marker, inspect precision before promoting"),

        (New-Channel main copilot swe_agent_bot author "author:copilot-swe-agent[bot]" "Copilot SWE agent bot slug; covers bot email alias"),
        (New-Channel main copilot msg_coauthored_by_copilot coauthor "`"Co-authored-by: Copilot`"" "broader Copilot co-author trailer"),
        (New-Channel audit copilot msg_noreply coauthor "`"copilot@users.noreply.github.com`"" "weekly audit: subset of Co-authored-by: Copilot"),
        (New-Channel audit copilot msg_github_copilot coauthor "`"Co-authored-by: GitHub Copilot`"" "weekly audit: low-volume specific trailer"),
        (New-Channel monthly_audit copilot discover_swe_agent marker "`"copilot-swe-agent`"" "monthly discovery audit: bot slug in messages or PR metadata"),

        (New-Channel main cursor cursoragent_email author "author-email:cursoragent@cursor.com" "main Cursor agent author email"),
        (New-Channel main cursor bg_agent_email author "author-email:agent@cursor.com" "Cursor background agent email"),
        (New-Channel main cursor bot_noreply author "author-email:206951365+cursor[bot]@users.noreply.github.com" "Cursor GitHub bot email"),
        (New-Channel main cursor msg_cursoragent coauthor "`"cursoragent@cursor.com`"" "Cursor co-author trailer email"),
        (New-Channel audit cursor msg_coauthored_by_cursor coauthor "`"Co-authored-by: Cursor`"" "weekly audit: near-complete overlap with cursoragent trailer"),
        (New-Channel monthly_audit cursor discover_cursor_agent marker "`"Cursor Agent`"" "monthly discovery audit: broad Cursor Agent marker, inspect precision before promoting")
    )

    $agentSet = @{}
    foreach ($agent in $Agents) {
        $agentSet[$agent.ToLowerInvariant()] = $true
    }
    $channels = @($channels | Where-Object { $agentSet.ContainsKey($_.agent) })
    if ($MaxChannels -gt 0) {
        $channels = @($channels | Select-Object -First $MaxChannels)
    }
    return $channels
}

function New-SampledWindows {
    param(
        [datetime]$Since,
        [datetime]$Until
    )
    $rng = [System.Random]::new($Seed)
    $windows = New-Object System.Collections.Generic.List[object]
    $day = $Since.Date
    while ($day -lt $Until) {
        if ($day.AddDays(1) -le $Since) {
            $day = $day.AddDays(1)
            continue
        }
        $bucketCount = [int][math]::Floor((24 * 60) / $SampleMinutesPerDay)
        $bucket = $rng.Next(0, $bucketCount)
        $start = $day.AddMinutes($bucket * $SampleMinutesPerDay)
        $end = $start.AddMinutes($SampleMinutesPerDay)
        if ($start -lt $Since) { $start = $Since }
        if ($end -gt $Until) { $end = $Until }
        if ($start -lt $end) {
            $windows.Add([pscustomobject]@{
                sample_date_utc = $day.ToString("yyyy-MM-dd")
                window_start_utc = Format-Utc $start
                window_end_utc = Format-Utc $end
                seed = $Seed
                bucket = $bucket
            })
        }
        $day = $day.AddDays(1)
    }
    if ($MaxDays -gt 0 -and $windows.Count -gt $MaxDays) {
        return @($windows.ToArray() | Select-Object -First $MaxDays)
    }
    return @($windows.ToArray())
}

function Get-WeekKey {
    param([string]$DateText)
    $date = [datetime]::ParseExact($DateText, "yyyy-MM-dd", [Globalization.CultureInfo]::InvariantCulture)
    $dayOffset = (([int]$date.DayOfWeek + 6) % 7)
    $weekStart = $date.AddDays(-1 * $dayOffset)
    return $weekStart.ToString("yyyy-MM-dd")
}

function New-WeeklyAuditSchedule {
    param([object[]]$Windows)
    $firstByWeek = @{}
    foreach ($window in ($Windows | Sort-Object sample_date_utc)) {
        $weekKey = Get-WeekKey -DateText $window.sample_date_utc
        if (-not $firstByWeek.ContainsKey($weekKey)) {
            $firstByWeek[$weekKey] = $window.sample_date_utc
        }
    }
    $rows = foreach ($weekKey in ($firstByWeek.Keys | Sort-Object)) {
        [pscustomobject]@{
            week_start_utc = $weekKey
            audit_sample_date_utc = $firstByWeek[$weekKey]
        }
    }
    return @($rows)
}

function New-MonthlyAuditSchedule {
    param([object[]]$Windows)
    $firstByMonth = @{}
    foreach ($window in ($Windows | Sort-Object sample_date_utc)) {
        $monthKey = $window.sample_date_utc.Substring(0, 7)
        if (-not $firstByMonth.ContainsKey($monthKey)) {
            $firstByMonth[$monthKey] = $window.sample_date_utc
        }
    }
    $rows = foreach ($monthKey in ($firstByMonth.Keys | Sort-Object)) {
        [pscustomobject]@{
            month_utc = $monthKey
            audit_sample_date_utc = $firstByMonth[$monthKey]
        }
    }
    return @($rows)
}

function Export-AppendCsv {
    param(
        [object[]]$Rows,
        [string]$Path
    )
    if (-not $Rows -or $Rows.Count -eq 0) { return }
    if (Test-Path -LiteralPath $Path) {
        $Rows | Export-Csv -LiteralPath $Path -NoTypeInformation -Append -Encoding UTF8
    } else {
        $Rows | Export-Csv -LiteralPath $Path -NoTypeInformation -Encoding UTF8
    }
}

function Export-AppendJsonlGzip {
    param(
        [object[]]$Rows,
        [string]$Path
    )
    if (-not $Rows -or $Rows.Count -eq 0) { return }
    $dir = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $dir | Out-Null

    $fs = [IO.File]::Open($Path, [IO.FileMode]::Append, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    try {
        $gzip = [IO.Compression.GZipStream]::new($fs, [IO.Compression.CompressionLevel]::Optimal)
        try {
            $encoding = [Text.UTF8Encoding]::new($false)
            $writer = [IO.StreamWriter]::new($gzip, $encoding)
            try {
                foreach ($row in $Rows) {
                    $writer.WriteLine(($row | ConvertTo-Json -Depth 100 -Compress))
                }
            } finally {
                $writer.Dispose()
            }
        } finally {
            if ($gzip) { $gzip.Dispose() }
        }
    } finally {
        $fs.Dispose()
    }
}

function Export-RawCommitItems {
    param(
        [object[]]$RawItems,
        [hashtable]$Seen,
        [string]$RawItemDir,
        [string]$IndexPath
    )
    if (-not $RawItems -or $RawItems.Count -eq 0) { return }

    $byMonth = @{}
    $indexRows = New-Object System.Collections.Generic.List[object]
    foreach ($raw in $RawItems) {
        if (-not $raw.repo_sha -or $Seen.ContainsKey($raw.repo_sha)) { continue }
        $Seen[$raw.repo_sha] = $true
        $month = $raw.original_window_start_utc.Substring(0, 7)
        if (-not $byMonth.ContainsKey($month)) {
            $byMonth[$month] = New-Object System.Collections.Generic.List[object]
        }
        $byMonth[$month].Add($raw) | Out-Null
        $indexRows.Add([pscustomobject]@{
            repo_sha = $raw.repo_sha
            month = $month
            raw_item_path = "raw_commit_items/raw_commit_items_$month.jsonl.gz"
            sha = $raw.sha
            repo = $raw.repo
            first_agent = $raw.agent
            first_tier = $raw.tier
            first_channel = $raw.channel
            first_window_start_utc = $raw.original_window_start_utc
        }) | Out-Null
    }

    foreach ($month in ($byMonth.Keys | Sort-Object)) {
        $path = Join-Path $RawItemDir "raw_commit_items_$month.jsonl.gz"
        Export-AppendJsonlGzip -Rows @($byMonth[$month].ToArray()) -Path $path
    }
    Export-AppendCsv -Rows @($indexRows.ToArray()) -Path $IndexPath
}

function Invoke-SearchWithRetry {
    param(
        [hashtable]$Headers,
        [string]$Query,
        [int]$Page
    )
    $encoded = [uri]::EscapeDataString($Query)
    $uri = "https://api.github.com/search/commits?q=$encoded&per_page=$PerPage&page=$Page&sort=author-date&order=desc"
    for ($attempt = 1; $attempt -le $MaxRetries; $attempt++) {
        try {
            $data = Invoke-RestMethod -Uri $uri -Headers $Headers -Method Get -TimeoutSec 45
            return [pscustomobject]@{ Data = $data; Error = ""; Attempts = $attempt }
        } catch {
            $msg = $_.Exception.Message
            if ($attempt -ge $MaxRetries) {
                return [pscustomobject]@{ Data = $null; Error = $msg; Attempts = $attempt }
            }
            $delay = $RetryBaseSeconds * $attempt
            Write-Warning "request failed page=$Page attempt=$attempt; sleeping ${delay}s; $msg"
            Start-Sleep -Seconds $delay
        }
    }
}

function New-QueryForWindow {
    param(
        [object]$Channel,
        [datetime]$Start,
        [datetime]$EndExclusive
    )
    $endInclusive = $EndExclusive.AddSeconds(-1)
    if ($endInclusive -lt $Start) {
        $endInclusive = $Start
    }
    return "$($Channel.query) author-date:$(Format-Utc $Start)..$(Format-Utc $endInclusive)"
}

function Fetch-ChannelWindow {
    param(
        [hashtable]$Headers,
        [object]$Channel,
        [datetime]$OriginalStart,
        [datetime]$OriginalEnd,
        [datetime]$Start,
        [datetime]$End,
        [int]$Depth
    )

    $query = New-QueryForWindow -Channel $Channel -Start $Start -EndExclusive $End
    $first = Invoke-SearchWithRetry -Headers $Headers -Query $query -Page 1
    $segments = New-Object System.Collections.Generic.List[object]
    $rows = New-Object System.Collections.Generic.List[object]
    $rawItems = New-Object System.Collections.Generic.List[object]

    $durationSeconds = [int]($End - $Start).TotalSeconds
    if ($first.Error) {
        $segments.Add([pscustomobject]@{
            tier = $Channel.tier
            agent = $Channel.agent
            channel = $Channel.channel
            mode = $Channel.mode
            original_window_start_utc = Format-Utc $OriginalStart
            original_window_end_utc = Format-Utc $OriginalEnd
            segment_start_utc = Format-Utc $Start
            segment_end_utc = Format-Utc $End
            depth = $Depth
            query = $Channel.query
            total_count = -1
            fetched_unique_sha = 0
            incomplete_results = $false
            cap_bound = $false
            status = "error"
            attempts = $first.Attempts
            error = $first.Error
        })
        return [pscustomobject]@{ Segments = @($segments.ToArray()); Rows = @($rows.ToArray()); RawItems = @($rawItems.ToArray()) }
    }

    $data = $first.Data
    $total = [int]$data.total_count
    $incomplete = [bool]$data.incomplete_results
    $maxReturnable = $PerPage * $MaxPages
    $wouldCap = $total -gt $maxReturnable
    $canSplit = $durationSeconds -gt $MinWindowSeconds

    if (($incomplete -or $wouldCap) -and $canSplit) {
        $segments.Add([pscustomobject]@{
            tier = $Channel.tier
            agent = $Channel.agent
            channel = $Channel.channel
            mode = $Channel.mode
            original_window_start_utc = Format-Utc $OriginalStart
            original_window_end_utc = Format-Utc $OriginalEnd
            segment_start_utc = Format-Utc $Start
            segment_end_utc = Format-Utc $End
            depth = $Depth
            query = $Channel.query
            total_count = $total
            fetched_unique_sha = 0
            incomplete_results = $incomplete
            cap_bound = $wouldCap
            status = "split_probe"
            attempts = $first.Attempts
            error = ""
        })
        $mid = $Start.AddSeconds([math]::Floor($durationSeconds / 2))
        $left = Fetch-ChannelWindow -Headers $Headers -Channel $Channel -OriginalStart $OriginalStart -OriginalEnd $OriginalEnd -Start $Start -End $mid -Depth ($Depth + 1)
        $right = Fetch-ChannelWindow -Headers $Headers -Channel $Channel -OriginalStart $OriginalStart -OriginalEnd $OriginalEnd -Start $mid -End $End -Depth ($Depth + 1)
        foreach ($s in @($left.Segments)) { $segments.Add($s) }
        foreach ($s in @($right.Segments)) { $segments.Add($s) }
        foreach ($r in @($left.Rows)) { $rows.Add($r) }
        foreach ($r in @($right.Rows)) { $rows.Add($r) }
        foreach ($r in @($left.RawItems)) { $rawItems.Add($r) }
        foreach ($r in @($right.RawItems)) { $rawItems.Add($r) }
        return [pscustomobject]@{ Segments = @($segments.ToArray()); Rows = @($rows.ToArray()); RawItems = @($rawItems.ToArray()) }
    }

    $seen = @{}
    $pageCount = [math]::Ceiling($total / $PerPage)
    if ($pageCount -lt 1) { $pageCount = 1 }
    if ($pageCount -gt $MaxPages) { $pageCount = $MaxPages }
    $pageErrors = New-Object System.Collections.Generic.List[string]
    $attempts = $first.Attempts

    for ($page = 1; $page -le $pageCount; $page++) {
        if ($page -eq 1) {
            $pageData = $data
        } else {
            Start-Sleep -Seconds $SleepSeconds
            $pageResult = Invoke-SearchWithRetry -Headers $Headers -Query $query -Page $page
            $attempts += $pageResult.Attempts
            if ($pageResult.Error) {
                $pageErrors.Add("page ${page}: $($pageResult.Error)")
                break
            }
            $pageData = $pageResult.Data
        }

        foreach ($item in @($pageData.items)) {
            $sha = [string]$item.sha
            $repo = ""
            if ($item.repository) { $repo = [string]$item.repository.full_name }
            $rowKey = "$repo|$sha"
            if (-not $sha -or $seen.ContainsKey($rowKey)) { continue }
            $seen[$rowKey] = $true
            $commit = $item.commit
            $author = $commit.author
            $committer = $commit.committer
            $message = [string]$commit.message
            $messageFirst = ""
            if ($message) { $messageFirst = ($message -split "`r?`n")[0] }
            $windowStartText = Format-Utc $OriginalStart
            $windowEndText = Format-Utc $OriginalEnd
            $segmentStartText = Format-Utc $Start
            $segmentEndText = Format-Utc $End
            $rows.Add([pscustomobject]@{
                tier = $Channel.tier
                agent = $Channel.agent
                channel = $Channel.channel
                mode = $Channel.mode
                original_window_start_utc = $windowStartText
                original_window_end_utc = $windowEndText
                segment_start_utc = $segmentStartText
                segment_end_utc = $segmentEndText
                sha = $sha
                repo = $repo
                repo_sha = $rowKey
                author_name = [string]$author.name
                author_email = [string]$author.email
                author_date = [string]$author.date
                committer_email = [string]$committer.email
                html_url = [string]$item.html_url
                message_first_line = $messageFirst
            })
            $rawItems.Add([pscustomobject]@{
                repo_sha = $rowKey
                sha = $sha
                repo = $repo
                tier = $Channel.tier
                agent = $Channel.agent
                channel = $Channel.channel
                mode = $Channel.mode
                query = $Channel.query
                original_window_start_utc = $windowStartText
                original_window_end_utc = $windowEndText
                segment_start_utc = $segmentStartText
                segment_end_utc = $segmentEndText
                page = $page
                item = $item
            })
        }
    }

    $terminalCap = ($total -gt $rows.Count) -and ($rows.Count -ge $maxReturnable)
    $status = "ok"
    if ($pageErrors.Count -gt 0) {
        $status = "error_partial"
    } elseif ($terminalCap) {
        $status = "cap_bound_terminal"
    } elseif ($incomplete) {
        $status = "incomplete_terminal"
    }
    $segments.Add([pscustomobject]@{
        tier = $Channel.tier
        agent = $Channel.agent
        channel = $Channel.channel
        mode = $Channel.mode
        original_window_start_utc = Format-Utc $OriginalStart
        original_window_end_utc = Format-Utc $OriginalEnd
        segment_start_utc = Format-Utc $Start
        segment_end_utc = Format-Utc $End
        depth = $Depth
        query = $Channel.query
        total_count = $total
        fetched_unique_sha = $rows.Count
        incomplete_results = $incomplete
        cap_bound = $terminalCap
        status = $status
        attempts = $attempts
        error = ($pageErrors.ToArray() -join " | ")
    })

    return [pscustomobject]@{ Segments = @($segments.ToArray()); Rows = @($rows.ToArray()); RawItems = @($rawItems.ToArray()) }
}

function Write-Summaries {
    param(
        [string]$OutputDir,
        [string]$RawPath,
        [string]$SegmentPath
    )
    if (-not (Test-Path -LiteralPath $RawPath)) {
        throw "No raw rows collected: $RawPath"
    }
    $rawRows = Import-Csv -LiteralPath $RawPath
    $segments = @()
    if (Test-Path -LiteralPath $SegmentPath) {
        $segments = Import-Csv -LiteralPath $SegmentPath
    }

    $channelSets = @{}
    $agentSets = @{}
    $uniqueRows = @{}
    foreach ($row in $rawRows) {
        $channelKey = "$($row.tier)|$($row.agent)|$($row.channel)|$($row.mode)"
        if (-not $channelSets.ContainsKey($channelKey)) { $channelSets[$channelKey] = @{} }
        $channelSets[$channelKey][$row.repo_sha] = $true

        if (-not $agentSets.ContainsKey($row.agent)) { $agentSets[$row.agent] = @{} }
        $agentSets[$row.agent][$row.repo_sha] = $true

        $uniqueKey = "$($row.agent)|$($row.repo_sha)"
        if (-not $uniqueRows.ContainsKey($uniqueKey)) {
            $uniqueRows[$uniqueKey] = [ordered]@{
                agent = $row.agent
                tiers = New-Object System.Collections.Generic.List[string]
                repo = $row.repo
                sha = $row.sha
                repo_sha = $row.repo_sha
                channels = New-Object System.Collections.Generic.List[string]
                modes = New-Object System.Collections.Generic.List[string]
                author_name = $row.author_name
                author_email = $row.author_email
                author_date = $row.author_date
                html_url = $row.html_url
                message_first_line = $row.message_first_line
            }
        }
        $uniqueRows[$uniqueKey].tiers.Add($row.tier)
        $uniqueRows[$uniqueKey].channels.Add($row.channel)
        $uniqueRows[$uniqueKey].modes.Add($row.mode)
    }

    $channelCounts = foreach ($key in ($channelSets.Keys | Sort-Object)) {
        $parts = $key -split "\|", 4
        $matchingSegments = @($segments | Where-Object { $_.tier -eq $parts[0] -and $_.agent -eq $parts[1] -and $_.channel -eq $parts[2] })
        $matchingRows = @($rawRows | Where-Object { $_.tier -eq $parts[0] -and $_.agent -eq $parts[1] -and $_.channel -eq $parts[2] })
        [pscustomobject]@{
            tier = $parts[0]
            agent = $parts[1]
            channel = $parts[2]
            mode = $parts[3]
            raw_rows_before_dedup = $matchingRows.Count
            unique_repo_sha_after_dedup = $channelSets[$key].Count
            segment_rows = $matchingSegments.Count
            total_count_sum = ($matchingSegments | Where-Object { $_.status -ne "split_probe" } | Measure-Object total_count -Sum).Sum
            fetched_unique_sha_sum = ($matchingSegments | Measure-Object fetched_unique_sha -Sum).Sum
            split_probe_segments = @($matchingSegments | Where-Object { $_.status -eq "split_probe" }).Count
            incomplete_segments = @($matchingSegments | Where-Object { $_.incomplete_results -eq "True" }).Count
            cap_bound_segments = @($matchingSegments | Where-Object { $_.cap_bound -eq "True" }).Count
            error_segments = @($matchingSegments | Where-Object { $_.status -like "error*" }).Count
        }
    }
    $channelCounts | Export-Csv -LiteralPath (Join-Path $OutputDir "channel_counts.csv") -NoTypeInformation -Encoding UTF8

    $agentCounts = foreach ($agent in ($agentSets.Keys | Sort-Object)) {
        $rawAgentRows = @($rawRows | Where-Object { $_.agent -eq $agent })
        $channelUniqueSum = ($channelCounts | Where-Object { $_.agent -eq $agent } | Measure-Object unique_repo_sha_after_dedup -Sum).Sum
        $dedupRatio = 0.0
        if ($channelUniqueSum -gt 0) { $dedupRatio = [math]::Round($agentSets[$agent].Count / $channelUniqueSum, 6) }
        [pscustomobject]@{
            agent = $agent
            raw_rows_before_dedup = $rawAgentRows.Count
            sum_channel_unique_before_cross_channel_dedup = $channelUniqueSum
            unique_repo_sha_after_dedup = $agentSets[$agent].Count
            cross_channel_dedup_ratio = $dedupRatio
        }
    }
    $agentCounts | Export-Csv -LiteralPath (Join-Path $OutputDir "agent_counts.csv") -NoTypeInformation -Encoding UTF8

    $uniqueCommitRows = foreach ($key in ($uniqueRows.Keys | Sort-Object)) {
        $entry = $uniqueRows[$key]
        [pscustomobject]@{
            agent = $entry.agent
            tiers = (($entry.tiers.ToArray() | Sort-Object -Unique) -join ";")
            repo = $entry.repo
            sha = $entry.sha
            repo_sha = $entry.repo_sha
            channels = (($entry.channels.ToArray() | Sort-Object -Unique) -join ";")
            modes = (($entry.modes.ToArray() | Sort-Object -Unique) -join ";")
            author_name = $entry.author_name
            author_email = $entry.author_email
            author_date = $entry.author_date
            html_url = $entry.html_url
            message_first_line = $entry.message_first_line
        }
    }
    $uniqueCommitRows | Export-Csv -LiteralPath (Join-Path $OutputDir "unique_commits.csv") -NoTypeInformation -Encoding UTF8

    $filterCounts = @()
    $filterCounts += [pscustomobject]@{
        scope = "all"
        raw_rows_before_dedup = $rawRows.Count
        unique_repo_sha_after_dedup = ($agentSets.Values | ForEach-Object { $_.Keys } | Sort-Object -Unique).Count
    }
    foreach ($row in $agentCounts) {
        $filterCounts += [pscustomobject]@{
            scope = "agent:$($row.agent)"
            raw_rows_before_dedup = $row.raw_rows_before_dedup
            unique_repo_sha_after_dedup = $row.unique_repo_sha_after_dedup
        }
    }
    foreach ($tier in ($rawRows | Select-Object -ExpandProperty tier -Unique | Sort-Object)) {
        $tierRows = @($rawRows | Where-Object { $_.tier -eq $tier })
        $tierSet = @{}
        foreach ($row in $tierRows) { $tierSet["$($row.agent)|$($row.repo_sha)"] = $true }
        $filterCounts += [pscustomobject]@{
            scope = "tier:$tier"
            raw_rows_before_dedup = $tierRows.Count
            unique_repo_sha_after_dedup = $tierSet.Count
        }
    }
    $filterCounts | Export-Csv -LiteralPath (Join-Path $OutputDir "filter_counts.csv") -NoTypeInformation -Encoding UTF8

    $keys = @($channelSets.Keys | Sort-Object)
    $pairs = New-Object System.Collections.Generic.List[object]
    for ($i = 0; $i -lt $keys.Count; $i++) {
        for ($j = $i + 1; $j -lt $keys.Count; $j++) {
            $leftKey = $keys[$i]
            $rightKey = $keys[$j]
            $left = $channelSets[$leftKey]
            $right = $channelSets[$rightKey]
            $leftParts = $leftKey -split "\|", 4
            $rightParts = $rightKey -split "\|", 4
            if ($leftParts[1] -ne $rightParts[1]) { continue }
            $intersection = 0
            foreach ($repoSha in $left.Keys) {
                if ($right.ContainsKey($repoSha)) { $intersection++ }
            }
            $union = $left.Count + $right.Count - $intersection
            $jaccard = 0.0
            if ($union -gt 0) { $jaccard = [math]::Round($intersection / $union, 6) }
            $pairs.Add([pscustomobject]@{
                agent = $leftParts[1]
                left_tier = $leftParts[0]
                left_channel = $leftParts[2]
                right_tier = $rightParts[0]
                right_channel = $rightParts[2]
                left_unique_repo_sha = $left.Count
                right_unique_repo_sha = $right.Count
                intersection_repo_sha = $intersection
                union_repo_sha = $union
                jaccard = $jaccard
            })
        }
    }
    $pairs | Export-Csv -LiteralPath (Join-Path $OutputDir "pairwise_overlap.csv") -NoTypeInformation -Encoding UTF8
}

$channels = @(Get-CompressedChannels)
if ($ListChannels) {
    $channels | Format-Table tier, agent, channel, mode, query, reason -AutoSize
    exit 0
}

$since = Parse-Utc $SinceUtc
$until = Parse-Utc $UntilUtc
if ($since -ge $until) { throw "SinceUtc must be earlier than UntilUtc." }

if (-not $OutputDir) {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH-mm-ssZ")
    $OutputDir = Join-Path (Join-Path (Split-Path -Parent $PSScriptRoot) "pilot") "annual_sample_$stamp"
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$token = Read-GitHubToken
$headers = @{
    "Accept" = "application/vnd.github.cloak-preview+json"
    "User-Agent" = "agent-may-sampled-collector"
}
if ($token) { $headers["Authorization"] = "token $token" }

$windows = @(New-SampledWindows -Since $since -Until $until)
$auditSchedule = @(New-WeeklyAuditSchedule -Windows $windows)
$monthlyAuditSchedule = @(New-MonthlyAuditSchedule -Windows $windows)
$auditDates = @{}
foreach ($row in $auditSchedule) {
    $auditDates[$row.audit_sample_date_utc] = $true
}
$monthlyAuditDates = @{}
foreach ($row in $monthlyAuditSchedule) {
    $monthlyAuditDates[$row.audit_sample_date_utc] = $true
}
$channelsPath = Join-Path $OutputDir "channels_compressed.csv"
$windowsPath = Join-Path $OutputDir "sampled_windows.csv"
$auditSchedulePath = Join-Path $OutputDir "weekly_audit_schedule.csv"
$monthlyAuditSchedulePath = Join-Path $OutputDir "monthly_audit_schedule.csv"
$segmentPath = Join-Path $OutputDir "query_segments.csv"
$rawPath = Join-Path $OutputDir "sha_channels_raw.csv"
$rawItemDir = Join-Path $OutputDir "raw_commit_items"
$rawItemIndexPath = Join-Path $OutputDir "raw_commit_items_index.csv"

$channels | Export-Csv -LiteralPath $channelsPath -NoTypeInformation -Encoding UTF8
$windows | Export-Csv -LiteralPath $windowsPath -NoTypeInformation -Encoding UTF8
$auditSchedule | Export-Csv -LiteralPath $auditSchedulePath -NoTypeInformation -Encoding UTF8
$monthlyAuditSchedule | Export-Csv -LiteralPath $monthlyAuditSchedulePath -NoTypeInformation -Encoding UTF8

$completed = @{}
if ($Resume -and (Test-Path -LiteralPath $segmentPath)) {
    foreach ($row in Import-Csv -LiteralPath $segmentPath) {
        if ($row.status -ne "error" -and $row.status -ne "error_partial" -and $row.status -ne "split_probe") {
            $completed["$($row.tier)|$($row.agent)|$($row.channel)|$($row.original_window_start_utc)|$($row.original_window_end_utc)"] = $true
        }
    }
}

$rawItemSeen = @{}
if ($Resume -and (Test-Path -LiteralPath $rawItemIndexPath)) {
    foreach ($row in Import-Csv -LiteralPath $rawItemIndexPath) {
        if ($row.repo_sha) { $rawItemSeen[$row.repo_sha] = $true }
    }
}

Write-Output "output_dir=$OutputDir"
Write-Output "range_utc=$(Format-Utc $since)..$(Format-Utc $until)"
Write-Output "sample_days=$($windows.Count) sample_minutes_per_day=$SampleMinutesPerDay seed=$Seed channels=$($channels.Count) main_channels=$(@($channels | Where-Object { $_.tier -eq 'main' }).Count) audit_channels=$(@($channels | Where-Object { $_.tier -eq 'audit' }).Count) monthly_audit_channels=$(@($channels | Where-Object { $_.tier -eq 'monthly_audit' }).Count) audit_days=$($auditSchedule.Count) monthly_audit_days=$($monthlyAuditSchedule.Count)"
Write-Output "per_page=$PerPage max_pages=$MaxPages min_window_seconds=$MinWindowSeconds sleep_seconds=$SleepSeconds"

foreach ($window in $windows) {
    $start = Parse-Utc $window.window_start_utc
    $end = Parse-Utc $window.window_end_utc
    foreach ($channel in $channels) {
        if ($channel.tier -eq "audit") {
            if ($DisableWeeklyAudit -or -not $auditDates.ContainsKey($window.sample_date_utc)) {
                continue
            }
        } elseif ($channel.tier -eq "monthly_audit") {
            if ($DisableMonthlyAudit -or -not $monthlyAuditDates.ContainsKey($window.sample_date_utc)) {
                continue
            }
        }
        $key = "$($channel.tier)|$($channel.agent)|$($channel.channel)|$($window.window_start_utc)|$($window.window_end_utc)"
        if ($completed.ContainsKey($key)) { continue }
        Write-Output "fetch day=$($window.sample_date_utc) $($window.window_start_utc)..$($window.window_end_utc) $($channel.tier)/$($channel.agent)/$($channel.channel)"
        $result = Fetch-ChannelWindow -Headers $headers -Channel $channel -OriginalStart $start -OriginalEnd $end -Start $start -End $end -Depth 0
        Export-AppendCsv -Rows @($result.Segments) -Path $segmentPath
        Export-AppendCsv -Rows @($result.Rows) -Path $rawPath
        Export-RawCommitItems -RawItems @($result.RawItems) -Seen $rawItemSeen -RawItemDir $rawItemDir -IndexPath $rawItemIndexPath
        $badSegments = @($result.Segments | Where-Object { $_.status -like "error*" -or $_.status -like "*terminal" })
        foreach ($bad in $badSegments) {
            Write-Warning "$($bad.status) $($channel.agent)/$($channel.channel) $($bad.segment_start_utc)..$($bad.segment_end_utc) total=$($bad.total_count) fetched=$($bad.fetched_unique_sha) $($bad.error)"
        }
        Start-Sleep -Seconds $SleepSeconds
    }
}

Write-Summaries -OutputDir $OutputDir -RawPath $rawPath -SegmentPath $segmentPath

Write-Output "wrote=$channelsPath"
Write-Output "wrote=$windowsPath"
Write-Output "wrote=$auditSchedulePath"
Write-Output "wrote=$monthlyAuditSchedulePath"
Write-Output "wrote=$segmentPath"
Write-Output "wrote=$rawPath"
Write-Output "wrote=$rawItemDir"
Write-Output "wrote=$rawItemIndexPath"
Write-Output "wrote=$(Join-Path $OutputDir 'channel_counts.csv')"
Write-Output "wrote=$(Join-Path $OutputDir 'agent_counts.csv')"
Write-Output "wrote=$(Join-Path $OutputDir 'filter_counts.csv')"
Write-Output "wrote=$(Join-Path $OutputDir 'unique_commits.csv')"
Write-Output "wrote=$(Join-Path $OutputDir 'pairwise_overlap.csv')"
