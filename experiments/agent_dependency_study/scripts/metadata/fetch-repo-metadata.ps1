<#
[IN]: merged commit population CSV and GITHUB_TOKEN.
[OUT]: data_products/repo_metadata_merged_1y_v1/ with repo_metadata.csv, repo_metadata_raw.jsonl, queue/status files, and manifest.json.
[POS]: Fetches repository-level metadata used for language joins, star/fork filters, and repo-level analysis.
[SYNC]: If metadata columns, queue semantics, or manifest names change, update scripts/CLAUDE.md and scripts/OUTPUTS.md.
#>param(
    [string]$PopulationCsv = "",
    [string]$OutputDir = "",
    [int]$MaxRepos = 20000,
    [double]$SleepSeconds = 1,
    [int]$MaxRetries = 5,
    [int]$RetryBaseSeconds = 30,
    [int]$RateLimitFloor = 50,
    [int]$RateLimitBufferSeconds = 30,
    [switch]$Resume,
    [switch]$QueueOnly
)

$ErrorActionPreference = "Stop"

function Resolve-DefaultPath {
    param([string]$Path, [string]$DefaultRelative)
    if ($Path) { return (Resolve-Path -LiteralPath $Path).Path }
    return (Resolve-Path -LiteralPath (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) $DefaultRelative)).Path
}

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

function Add-Token {
    param([string]$Current, [string]$Token)
    if (-not $Token) { return $Current }
    if (-not $Current) { return $Token }
    $needle = ";$Token;"
    $haystack = ";$Current;"
    if ($haystack.Contains($needle)) { return $Current }
    return "$Current;$Token"
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

function Append-Jsonl {
    param(
        [object]$Object,
        [string]$Path
    )
    $json = $Object | ConvertTo-Json -Depth 100 -Compress
    Add-Content -LiteralPath $Path -Value $json -Encoding UTF8
}

function Get-HeaderValue {
    param(
        [object]$Headers,
        [string]$Name
    )
    if (-not $Headers) { return "" }
    try {
        $value = $Headers[$Name]
        if ($null -eq $value) { return "" }
        if ($value -is [array]) { return [string]$value[0] }
        return [string]$value
    } catch {
        return ""
    }
}

function Get-RateLimitSnapshot {
    param([object]$Headers)
    $limitText = Get-HeaderValue -Headers $Headers -Name "X-RateLimit-Limit"
    $remainingText = Get-HeaderValue -Headers $Headers -Name "X-RateLimit-Remaining"
    $resetText = Get-HeaderValue -Headers $Headers -Name "X-RateLimit-Reset"
    $resource = Get-HeaderValue -Headers $Headers -Name "X-RateLimit-Resource"
    $retryAfterText = Get-HeaderValue -Headers $Headers -Name "Retry-After"

    $limit = -1
    $remaining = -1
    $resetEpoch = 0
    $retryAfter = 0
    [void][int]::TryParse($limitText, [ref]$limit)
    [void][int]::TryParse($remainingText, [ref]$remaining)
    [void][int64]::TryParse($resetText, [ref]$resetEpoch)
    [void][int]::TryParse($retryAfterText, [ref]$retryAfter)

    $resetLocal = ""
    if ($resetEpoch -gt 0) {
        $resetLocal = [DateTimeOffset]::FromUnixTimeSeconds($resetEpoch).ToLocalTime().ToString("yyyy-MM-dd HH:mm:ss zzz")
    }

    return [pscustomobject]@{
        Limit = $limit
        Remaining = $remaining
        ResetEpoch = $resetEpoch
        ResetLocal = $resetLocal
        Resource = $resource
        RetryAfter = $retryAfter
    }
}

function Get-SecondsUntilReset {
    param([object]$Rate)
    if ($Rate.RetryAfter -gt 0) {
        return $Rate.RetryAfter + $RateLimitBufferSeconds
    }
    if ($Rate.ResetEpoch -le 0) {
        return 0
    }
    $nowEpoch = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $seconds = [int]($Rate.ResetEpoch - $nowEpoch + $RateLimitBufferSeconds)
    if ($seconds -lt 0) { return 0 }
    return $seconds
}

function Wait-IfRateLimitLow {
    param(
        [object]$Rate,
        [string]$Context
    )
    if (-not $Rate) { return }
    if ($Rate.Remaining -ge 0 -and $Rate.Remaining -le $RateLimitFloor) {
        $sleep = Get-SecondsUntilReset -Rate $Rate
        if ($sleep -gt 0) {
            Write-Output "rate_limit_wait context=$Context resource=$($Rate.Resource) remaining=$($Rate.Remaining) limit=$($Rate.Limit) reset_local=$($Rate.ResetLocal) sleep_seconds=$sleep"
            Start-Sleep -Seconds $sleep
        }
    }
}

function Sleep-FractionalSeconds {
    param([double]$Seconds)
    if ($Seconds -le 0) { return }
    $milliseconds = [int][math]::Ceiling($Seconds * 1000)
    Start-Sleep -Milliseconds $milliseconds
}

function Invoke-GitHubWithRetry {
    param(
        [hashtable]$Headers,
        [string]$Uri
    )
    for ($attempt = 1; $attempt -le $MaxRetries; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri $Uri -Headers $Headers -Method Get -TimeoutSec 60 -UseBasicParsing
            $rate = Get-RateLimitSnapshot -Headers $response.Headers
            $data = $response.Content | ConvertFrom-Json
            return [pscustomobject]@{ Data = $data; Error = ""; Status = "ok"; Attempts = $attempt; Rate = $rate }
        } catch {
            $msg = $_.Exception.Message
            $statusCode = 0
            $rate = $null
            try {
                if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
                    $statusCode = [int]$_.Exception.Response.StatusCode
                    $rate = Get-RateLimitSnapshot -Headers $_.Exception.Response.Headers
                }
            } catch {
                $statusCode = 0
            }
            if ($statusCode -eq 404) {
                return [pscustomobject]@{ Data = $null; Error = $msg; Status = "not_found"; Attempts = $attempt; Rate = $rate }
            }
            if ($statusCode -eq 410) {
                return [pscustomobject]@{ Data = $null; Error = $msg; Status = "gone"; Attempts = $attempt; Rate = $rate }
            }
            if ($statusCode -eq 403 -and $rate -and $rate.Remaining -eq 0) {
                $delay = Get-SecondsUntilReset -Rate $rate
                if ($delay -gt 0) {
                    Write-Warning "core rate limit exhausted attempt=$attempt; sleeping ${delay}s until reset=$($rate.ResetLocal); $msg"
                    Start-Sleep -Seconds $delay
                    continue
                }
            }
            if ($attempt -ge $MaxRetries) {
                return [pscustomobject]@{ Data = $null; Error = $msg; Status = "error"; Attempts = $attempt; Rate = $rate }
            }
            $delay = $RetryBaseSeconds * $attempt
            Write-Warning "repo request failed status=$statusCode attempt=$attempt; sleeping ${delay}s; $msg"
            Start-Sleep -Seconds $delay
        }
    }
}

function Build-RepoQueue {
    param(
        [string]$PopulationPath,
        [string]$QueuePath
    )
    $repos = @{}
    $rows = 0
    Write-Output "scan_population=$PopulationPath"
    Import-Csv -LiteralPath $PopulationPath | ForEach-Object {
        $repo = [string]$_.repo
        if (-not $repo) { return }
        if (-not $repos.ContainsKey($repo)) {
            $repos[$repo] = [ordered]@{
                repo = $repo
                agent_commit_rows = 0
                agents = ""
                first_author_date = ""
                last_author_date = ""
            }
        }
        $entry = $repos[$repo]
        $entry.agent_commit_rows++
        $entry.agents = Add-Token $entry.agents ([string]$_.agent)
        $date = [string]$_.author_date
        if ($date) {
            if (-not $entry.first_author_date -or $date -lt $entry.first_author_date) {
                $entry.first_author_date = $date
            }
            if (-not $entry.last_author_date -or $date -gt $entry.last_author_date) {
                $entry.last_author_date = $date
            }
        }
        $rows++
    }

    $queueRows = foreach ($repo in $repos.Keys) {
        $entry = $repos[$repo]
        [pscustomobject]@{
            repo = $entry.repo
            agent_commit_rows = $entry.agent_commit_rows
            agents = $entry.agents
            first_author_date = $entry.first_author_date
            last_author_date = $entry.last_author_date
        }
    }
    $queueRows |
        Sort-Object @{Expression = { [int]$_.agent_commit_rows }; Descending = $true}, repo |
        Export-Csv -LiteralPath $QueuePath -NoTypeInformation -Encoding UTF8
    Write-Output "queue_repos=$($repos.Count) population_rows=$rows wrote=$QueuePath"
}

$populationPath = Resolve-DefaultPath -Path $PopulationCsv -DefaultRelative "data_products\agent_commit_population_merged_1y_v1\merged_agent_commit_population.csv"
if (-not $OutputDir) {
    $OutputDir = Join-Path (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) "data_products") "repo_metadata_merged_1y_v1"
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$queuePath = Join-Path $OutputDir "repo_queue.csv"
$metadataPath = Join-Path $OutputDir "repo_metadata.csv"
$rawJsonlPath = Join-Path $OutputDir "repo_metadata_raw.jsonl"
$manifestPath = Join-Path $OutputDir "manifest.json"

if (-not (Test-Path -LiteralPath $queuePath)) {
    Build-RepoQueue -PopulationPath $populationPath -QueuePath $queuePath
} else {
    Write-Output "queue_exists=$queuePath"
}

if ($QueueOnly) {
    Write-Output "queue_only=true"
    exit 0
}

$completed = @{}
if (Test-Path -LiteralPath $metadataPath) {
    Import-Csv -LiteralPath $metadataPath | ForEach-Object {
        if ($_.repo) { $completed[[string]$_.repo] = $true }
    }
    Write-Output "existing_metadata_rows=$($completed.Count); existing repos will be skipped"
}

$token = Read-GitHubToken
$headers = @{
    "Accept" = "application/vnd.github+json"
    "X-GitHub-Api-Version" = "2022-11-28"
    "User-Agent" = "agent-commit-repo-metadata-fetcher"
}
if ($token) { $headers["Authorization"] = "token $token" }

$fetched = 0
$skipped = 0
$errors = 0
$lastRate = $null
$queue = Import-Csv -LiteralPath $queuePath
if ($MaxRepos -gt 0) {
    $queue = @($queue | Select-Object -First $MaxRepos)
}

Write-Output "output_dir=$OutputDir"
Write-Output "queue_count=$(@($queue).Count) max_repos=$MaxRepos resume=$Resume sleep_seconds=$SleepSeconds"
Write-Output "metadata_path=$metadataPath"

foreach ($row in $queue) {
    $repo = [string]$row.repo
    if (-not $repo) { continue }
    if ($completed.ContainsKey($repo)) {
        $skipped++
        continue
    }
    if ($repo -notmatch "^([^/]+)/([^/]+)$") {
        $errors++
        Export-AppendCsv -Rows @([pscustomobject]@{
            repo = $repo
            status = "invalid_repo_name"
            agent_commit_rows = [string]$row.agent_commit_rows
            agents = [string]$row.agents
            id = ""
            full_name = ""
            owner_login = ""
            owner_type = ""
            private = ""
            fork = ""
            archived = ""
            disabled = ""
            size = ""
            stargazers_count = ""
            forks_count = ""
            open_issues_count = ""
            watchers_count = ""
            language = ""
            created_at = ""
            updated_at = ""
            pushed_at = ""
            default_branch = ""
            license_key = ""
            parent_full_name = ""
            source_full_name = ""
            error = "invalid repo name"
        }) -Path $metadataPath
        continue
    }

    $owner = $Matches[1]
    $name = $Matches[2]
    $uri = "https://api.github.com/repos/$owner/$name"
    Write-Output "fetch_repo $repo"
    $result = Invoke-GitHubWithRetry -Headers $headers -Uri $uri
    if ($result.Rate) { $lastRate = $result.Rate }
    if ($result.Error) {
        $errors++
        $status = [string]$result.Status
        if (-not $status) { $status = "error" }
        Export-AppendCsv -Rows @([pscustomobject]@{
            repo = $repo
            status = $status
            agent_commit_rows = [string]$row.agent_commit_rows
            agents = [string]$row.agents
            id = ""
            full_name = ""
            owner_login = ""
            owner_type = ""
            private = ""
            fork = ""
            archived = ""
            disabled = ""
            size = ""
            stargazers_count = ""
            forks_count = ""
            open_issues_count = ""
            watchers_count = ""
            language = ""
            created_at = ""
            updated_at = ""
            pushed_at = ""
            default_branch = ""
            license_key = ""
            parent_full_name = ""
            source_full_name = ""
            error = $result.Error
        }) -Path $metadataPath
    } else {
        $data = $result.Data
        $licenseKey = ""
        if ($data.license) { $licenseKey = [string]$data.license.key }
        $parentFull = ""
        if ($data.parent) { $parentFull = [string]$data.parent.full_name }
        $sourceFull = ""
        if ($data.source) { $sourceFull = [string]$data.source.full_name }
        Export-AppendCsv -Rows @([pscustomobject]@{
            repo = $repo
            status = "ok"
            agent_commit_rows = [string]$row.agent_commit_rows
            agents = [string]$row.agents
            id = [string]$data.id
            full_name = [string]$data.full_name
            owner_login = [string]$data.owner.login
            owner_type = [string]$data.owner.type
            private = [string]$data.private
            fork = [string]$data.fork
            archived = [string]$data.archived
            disabled = [string]$data.disabled
            size = [string]$data.size
            stargazers_count = [string]$data.stargazers_count
            forks_count = [string]$data.forks_count
            open_issues_count = [string]$data.open_issues_count
            watchers_count = [string]$data.watchers_count
            language = [string]$data.language
            created_at = [string]$data.created_at
            updated_at = [string]$data.updated_at
            pushed_at = [string]$data.pushed_at
            default_branch = [string]$data.default_branch
            license_key = $licenseKey
            parent_full_name = $parentFull
            source_full_name = $sourceFull
            error = ""
        }) -Path $metadataPath
        Append-Jsonl -Object $data -Path $rawJsonlPath
        $fetched++
        Wait-IfRateLimitLow -Rate $result.Rate -Context "after_success"
    }

    if (($fetched + $errors) % 100 -eq 0) {
        if ($lastRate) {
            Write-Output "progress fetched=$fetched errors=$errors skipped=$skipped rate_remaining=$($lastRate.Remaining) rate_limit=$($lastRate.Limit) reset_local=$($lastRate.ResetLocal)"
        } else {
            Write-Output "progress fetched=$fetched errors=$errors skipped=$skipped"
        }
    }
    Sleep-FractionalSeconds -Seconds $SleepSeconds
}

$manifest = [pscustomobject]@{
    output_dir = $OutputDir
    population_csv = $populationPath
    queue_path = $queuePath
    metadata_path = $metadataPath
    raw_jsonl_path = $rawJsonlPath
    max_repos = $MaxRepos
    fetched_this_run = $fetched
    errors_this_run = $errors
    skipped_this_run = $skipped
    sleep_seconds = $SleepSeconds
    rate_limit_floor = $RateLimitFloor
    rate_limit_buffer_seconds = $RateLimitBufferSeconds
    last_rate_limit_remaining = if ($lastRate) { $lastRate.Remaining } else { "" }
    last_rate_limit_limit = if ($lastRate) { $lastRate.Limit } else { "" }
    last_rate_limit_reset_local = if ($lastRate) { $lastRate.ResetLocal } else { "" }
    created_utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

Write-Output "done fetched=$fetched errors=$errors skipped=$skipped"
Write-Output "wrote=$queuePath"
Write-Output "wrote=$metadataPath"
Write-Output "wrote=$rawJsonlPath"
Write-Output "wrote=$manifestPath"
