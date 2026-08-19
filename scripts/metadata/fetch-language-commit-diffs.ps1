<#
[IN]: language list, merged commit population CSV, repo metadata CSV, GITHUB_TOKEN, and optional output root.
[OUT]: <THESIS_DATA_ROOT>/diff_corpus/commit_diffs_by_language/<Language>/*.diff plus manifest/index CSV files.
[POS]: Builds a large raw diff corpus by repository primary language for downstream RQ3 exploration.
[SYNC]: If output root, manifest columns, or language filtering semantics change, update scripts/CLAUDE.md and scripts/OUTPUTS.md.
#>param(
    [Parameter(Mandatory = $true)]
    [string[]]$Languages,
    [string]$CommitCsv = "",
    [string]$RepoMetadataCsv = "",
    [string]$OutputRoot = "",
    [int]$MaxCommits = 0,
    [double]$SleepSeconds = 0.5,
    [int]$MaxRetries = 5,
    [int]$RetryBaseSeconds = 60,
    [int]$RateLimitFloor = 50,
    [int]$RateLimitBufferSeconds = 30,
    [switch]$LogSkipped,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

function Resolve-DefaultPath {
    param([string]$Path, [string]$DefaultRelative)
    if ($Path) { return (Resolve-Path -LiteralPath $Path).Path }
    $root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    return (Resolve-Path -LiteralPath (Join-Path $root $DefaultRelative)).Path
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

function ConvertTo-SafeSegment {
    param([string]$Value)
    $safe = $Value -replace '[<>:"/\\|?*]', '_'
    $safe = $safe -replace '\s+', '_'
    $safe = $safe.Trim(" ._")
    if (-not $safe) { return "_blank" }
    return $safe
}

function Export-AppendCsv {
    param([object[]]$Rows, [string]$Path)
    if (-not $Rows -or $Rows.Count -eq 0) { return }
    if (Test-Path -LiteralPath $Path) {
        $Rows | Export-Csv -LiteralPath $Path -NoTypeInformation -Append -Encoding UTF8
    } else {
        $Rows | Export-Csv -LiteralPath $Path -NoTypeInformation -Encoding UTF8
    }
}

function ConvertTo-ResponseText {
    param([object]$Content)
    if ($null -eq $Content) { return "" }
    if ($Content -is [byte[]]) {
        $utf8 = [System.Text.UTF8Encoding]::new($false, $true)
        return $utf8.GetString($Content)
    }
    if ($Content -is [string]) { return $Content }
    return [string]$Content
}

function Get-ResponseContentLength {
    param([object]$Content)
    if ($null -eq $Content) { return 0 }
    if ($Content -is [byte[]]) { return $Content.Length }
    if ($Content -is [string]) { return ([System.Text.UTF8Encoding]::new($false)).GetByteCount($Content) }
    return ([System.Text.UTF8Encoding]::new($false)).GetByteCount([string]$Content)
}

function Get-HeaderValue {
    param([object]$Headers, [string]$Name)
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
    $remaining = -1
    $limit = -1
    $resetEpoch = 0
    [void][int]::TryParse((Get-HeaderValue $Headers "X-RateLimit-Remaining"), [ref]$remaining)
    [void][int]::TryParse((Get-HeaderValue $Headers "X-RateLimit-Limit"), [ref]$limit)
    [void][int64]::TryParse((Get-HeaderValue $Headers "X-RateLimit-Reset"), [ref]$resetEpoch)
    return [pscustomobject]@{ Remaining = $remaining; Limit = $limit; ResetEpoch = $resetEpoch }
}

function Get-SecondsUntilReset {
    param([object]$Rate)
    if (-not $Rate -or $Rate.ResetEpoch -le 0) { return 0 }
    $nowEpoch = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $seconds = [int]($Rate.ResetEpoch - $nowEpoch + $RateLimitBufferSeconds)
    if ($seconds -lt 0) { return 0 }
    return $seconds
}

function Wait-IfRateLimitLow {
    param([object]$Rate)
    if (-not $Rate) { return }
    if ($Rate.Remaining -ge 0 -and $Rate.Remaining -le $RateLimitFloor) {
        $sleep = Get-SecondsUntilReset -Rate $Rate
        if ($sleep -gt 0) {
            Write-Output "rate_limit_wait remaining=$($Rate.Remaining) limit=$($Rate.Limit) sleep_seconds=$sleep"
            Start-Sleep -Seconds $sleep
        }
    }
}

function Sleep-FractionalSeconds {
    param([double]$Seconds)
    if ($Seconds -le 0) { return }
    Start-Sleep -Milliseconds ([int][math]::Ceiling($Seconds * 1000))
}

function Get-DiffPath {
    param([string]$LanguageRoot, [string]$Repo, [string]$Sha)
    $repoDir = ConvertTo-SafeSegment ($Repo -replace '/', '__')
    return Join-Path (Join-Path $LanguageRoot $repoDir) "$Sha.diff"
}

function Invoke-GitHubDiff {
    param([hashtable]$Headers, [string]$Uri)
    for ($attempt = 1; $attempt -le $MaxRetries; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri $Uri -Headers $Headers -Method Get -TimeoutSec 120 -UseBasicParsing
            $rate = Get-RateLimitSnapshot $response.Headers
            return [pscustomobject]@{
                Content = ConvertTo-ResponseText $response.Content
                ContentLengthBytes = Get-ResponseContentLength $response.Content
                Status = "ok"
                HttpStatus = [int]$response.StatusCode
                Error = ""
                Rate = $rate
            }
        } catch {
            $statusCode = Get-HttpStatusCode $_
            $rate = Get-ErrorRateLimit $_
            if ($statusCode -in 403, 429) { Wait-ForRetry -Rate $rate -Attempt $attempt }
            if ($attempt -ge $MaxRetries -or $statusCode -in 404, 409, 422) {
                return [pscustomobject]@{
                    Content = ""
                    ContentLengthBytes = 0
                    Status = "error_$statusCode"
                    HttpStatus = $statusCode
                    Error = $_.Exception.Message
                    Rate = $rate
                }
            }
            Start-Sleep -Seconds ($RetryBaseSeconds * $attempt)
        }
    }
}

function Get-HttpStatusCode {
    param([object]$ErrorRecord)
    try {
        if ($ErrorRecord.Exception.Response.StatusCode) {
            return [int]$ErrorRecord.Exception.Response.StatusCode
        }
    } catch {
        return 0
    }
    return 0
}

function Get-ErrorRateLimit {
    param([object]$ErrorRecord)
    try {
        if ($ErrorRecord.Exception.Response.Headers) {
            return Get-RateLimitSnapshot $ErrorRecord.Exception.Response.Headers
        }
    } catch {
        return $null
    }
    return $null
}

function Wait-ForRetry {
    param([object]$Rate, [int]$Attempt)
    $sleep = Get-SecondsUntilReset -Rate $Rate
    if ($sleep -le 0) { $sleep = $RetryBaseSeconds * $Attempt }
    Write-Warning "request limited; sleeping ${sleep}s"
    Start-Sleep -Seconds $sleep
}

function Build-LanguageSet {
    param([string[]]$Values)
    $set = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($value in $Values) {
        if ($value -eq "<blank>") { [void]$set.Add("") } else { [void]$set.Add($value) }
    }
    return $set
}

function Read-RepoLanguages {
    param([string]$Path, [object]$LanguageSet)
    $map = @{}
    Import-Csv -LiteralPath $Path | ForEach-Object {
        if ($_.status -ne "ok") { return }
        $repo = [string]$_.repo
        $language = [string]$_.language
        if ($repo -and $LanguageSet.Contains($language)) { $map[$repo] = $language }
    }
    return $map
}

function New-IndexRow {
    param([object]$Row, [string]$Language, [string]$Status, [string]$Path, [string]$Error, [object]$Result)
    return [pscustomobject]@{
        language = if ($Language) { $Language } else { "<blank>" }
        repo = [string]$Row.repo
        sha = [string]$Row.sha
        status = $Status
        http_status = if ($Result) { $Result.HttpStatus } else { "" }
        diff_size_bytes = if ($Result) { $Result.ContentLengthBytes } else { "" }
        diff_path = $Path
        html_url = [string]$Row.html_url
        error = $Error
        fetched_utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    }
}

function Write-Manifest {
    param([object]$Manifest, [string]$Path)
    $Manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $Path -Encoding UTF8
}

$commitPath = Resolve-DefaultPath -Path $CommitCsv -DefaultRelative "data_products\agent_commit_population_merged_1y_v1\merged_agent_commit_population.csv"
$repoMetadataPath = Resolve-DefaultPath -Path $RepoMetadataCsv -DefaultRelative "data_products\repo_metadata_merged_1y_v1\repo_metadata.csv"
$languageSet = Build-LanguageSet -Values $Languages
$token = Read-GitHubToken

if (-not $token) { throw "GITHUB_TOKEN not found in scripts\.env or environment." }
if (-not $OutputRoot) {
    $repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $dataRoot = if ($env:THESIS_DATA_ROOT) {
        $env:THESIS_DATA_ROOT
    } else {
        Join-Path $repoRoot "data_external"
    }
    $OutputRoot = Join-Path $dataRoot "diff_corpus\commit_diffs_by_language"
}
New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null

Write-Output "commit_csv=$commitPath"
Write-Output "repo_metadata_csv=$repoMetadataPath"
Write-Output "output_root=$OutputRoot"
Write-Output "languages=$($Languages -join ',')"

$repoLanguage = Read-RepoLanguages -Path $repoMetadataPath -LanguageSet $languageSet
Write-Output "matching_repos=$($repoLanguage.Count)"

$headers = @{
    "Accept" = "application/vnd.github.diff"
    "X-GitHub-Api-Version" = "2022-11-28"
    "User-Agent" = "language-commit-diff-fetcher"
    "Authorization" = "token $token"
}

$runStamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$manifestDir = Join-Path $OutputRoot "manifests"
New-Item -ItemType Directory -Force -Path $manifestDir | Out-Null

$fetched = 0
$skipped = 0
$errors = 0
$examined = 0
$matched = 0
$seen = [System.Collections.Generic.HashSet[string]]::new()

Import-Csv -LiteralPath $commitPath | ForEach-Object {
    if ($MaxCommits -gt 0 -and ($fetched + $errors + $skipped) -ge $MaxCommits) { break }
    $examined++
    $repo = [string]$_.repo
    $sha = [string]$_.sha
    if (-not $repo -or -not $sha -or -not $repoLanguage.ContainsKey($repo)) { return }

    $key = "$repo@$sha"
    if (-not $seen.Add($key)) { return }
    $matched++

    $language = $repoLanguage[$repo]
    $languageRoot = Join-Path $OutputRoot (ConvertTo-SafeSegment $language)
    $diffPath = Get-DiffPath -LanguageRoot $languageRoot -Repo $repo -Sha $sha
    $indexPath = Join-Path $languageRoot "diff_index.csv"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $diffPath) | Out-Null

    if ((Test-Path -LiteralPath $diffPath) -and -not $Force) {
        $skipped++
        if ($LogSkipped) {
            Export-AppendCsv -Rows @(New-IndexRow $_ $language "skipped_existing" $diffPath "" $null) -Path $indexPath
        }
        return
    }

    $uri = "https://api.github.com/repos/$repo/commits/$sha"
    Write-Output "fetch_diff language=$language repo=$repo sha=$sha"
    $result = Invoke-GitHubDiff -Headers $headers -Uri $uri
    if ($result.Status -eq "ok") {
        $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
        [System.IO.File]::WriteAllText($diffPath, $result.Content, $utf8NoBom)
        $fetched++
        Export-AppendCsv -Rows @(New-IndexRow $_ $language "ok" $diffPath "" $result) -Path $indexPath
        Wait-IfRateLimitLow -Rate $result.Rate
    } else {
        $errors++
        Export-AppendCsv -Rows @(New-IndexRow $_ $language $result.Status "" $result.Error $result) -Path $indexPath
    }

    if (($fetched + $errors + $skipped) % 100 -eq 0) {
        Write-Output "progress matched=$matched fetched=$fetched skipped=$skipped errors=$errors examined=$examined"
    }
    Sleep-FractionalSeconds -Seconds $SleepSeconds
}

$manifestPath = Join-Path $manifestDir "diff_fetch_$runStamp.json"
$manifest = [pscustomobject]@{
    commit_csv = $commitPath
    repo_metadata_csv = $repoMetadataPath
    output_root = $OutputRoot
    languages = $Languages
    max_commits = $MaxCommits
    sleep_seconds = $SleepSeconds
    log_skipped = [bool]$LogSkipped
    force = [bool]$Force
    examined_rows = $examined
    matched_unique_repo_sha = $matched
    fetched = $fetched
    skipped = $skipped
    errors = $errors
    created_utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}
Write-Manifest -Manifest $manifest -Path $manifestPath

Write-Output "done matched=$matched fetched=$fetched skipped=$skipped errors=$errors"
Write-Output "manifest=$manifestPath"
