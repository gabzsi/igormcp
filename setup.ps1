[CmdletBinding()]
param(
    [string]$Python,
    [switch]$SkipKnowledgeDownload,
    [string]$IncludeExistingKnowledge
)

$ErrorActionPreference = "Stop"

function Invoke-ExternalCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Description
    )

    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Get-PythonVersion {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$PrefixArguments
    )

    $arguments = @($PrefixArguments) + @(
        "-c",
        "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    )
    $versionText = & $Executable @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Could not run Python at '$Executable'."
    }

    try {
        return [version]($versionText.Trim() + ".0")
    }
    catch {
        throw "Could not parse Python version output: $versionText"
    }
}

function ConvertTo-TomlString {
    param([Parameter(Mandatory = $true)][string]$Value)

    $escaped = $Value.Replace('\', '\\').Replace('"', '\"')
    return '"' + $escaped + '"'
}

function Test-CompleteKnowledgeBundle {
    param([Parameter(Mandatory = $true)][string]$Directory)

    $manifestPath = Join-Path $Directory "manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        return $false
    }

    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    }
    catch {
        return $false
    }

    if ($null -eq $manifest.documents -or $manifest.documents.Count -lt 1) {
        return $false
    }
    $manuals = @($manifest.documents | Where-Object { $_.collection -eq "manuals" })
    if ($manuals.Count -lt 1) {
        return $false
    }

    $root = (Resolve-Path -LiteralPath $Directory).Path.TrimEnd('\') + '\'
    foreach ($document in $manifest.documents) {
        $relativePath = [string]$document.path
        if ([string]::IsNullOrWhiteSpace($relativePath) -or [System.IO.Path]::IsPathRooted($relativePath)) {
            return $false
        }
        $parts = $relativePath -split '[\\/]'
        if ($parts.Count -eq 0 -or @($parts | Where-Object { $_ -in @('', '.', '..') }).Count -gt 0) {
            return $false
        }
        $candidate = Join-Path $Directory ($relativePath -replace '/', '\')
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $false
        }
        $resolvedCandidate = (Resolve-Path -LiteralPath $candidate).Path
        if (-not $resolvedCandidate.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $false
        }
        $expectedHash = [string]$document.bundle_sha256
        if ($expectedHash -notmatch '^[0-9A-Fa-f]{64}$') {
            return $false
        }
        $actualHash = (Get-FileHash -LiteralPath $resolvedCandidate -Algorithm SHA256).Hash
        if (-not $actualHash.Equals($expectedHash, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $false
        }
    }

    return $true
}

$repoRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$venvDirectory = Join-Path $repoRoot ".venv"
$venvPython = Join-Path $venvDirectory "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    if (Test-Path -LiteralPath $venvDirectory) {
        throw "A .venv directory already exists but has no Scripts\python.exe. Preserve it and repair it manually, or move it aside before rerunning setup."
    }

    $basePython = $null
    $baseArguments = @()
    if (-not [string]::IsNullOrWhiteSpace($Python)) {
        if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
            throw "The Python executable does not exist: $Python"
        }
        $basePython = (Resolve-Path -LiteralPath $Python).Path
    }
    else {
        $launcher = Get-Command "py" -ErrorAction SilentlyContinue
        if ($null -ne $launcher) {
            $basePython = $launcher.Source
            $baseArguments = @("-3")
        }
        else {
            $pythonCommand = Get-Command "python" -ErrorAction SilentlyContinue
            if ($null -eq $pythonCommand) {
                throw "Python 3.10 or newer was not found. Install Python or rerun with -Python 'C:\path\to\python.exe'."
            }
            $basePython = $pythonCommand.Source
        }
    }

    $baseVersion = Get-PythonVersion -Executable $basePython -PrefixArguments $baseArguments
    if ($baseVersion -lt [version]"3.10.0") {
        throw "Python 3.10 or newer is required; found $baseVersion at '$basePython'."
    }

    Write-Host "Creating project-local virtual environment with Python $baseVersion..."
    $createArguments = @($baseArguments) + @("-m", "venv", $venvDirectory)
    Invoke-ExternalCommand -Executable $basePython -Arguments $createArguments -Description "Creating .venv"
}

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "The project environment was not created at '$venvPython'."
}

$venvVersion = Get-PythonVersion -Executable $venvPython -PrefixArguments @()
if ($venvVersion -lt [version]"3.10.0") {
    throw "Python 3.10 or newer is required in .venv; found $venvVersion."
}

Write-Host "Installing Igor Pro MCP and its declared dependencies into .venv..."
Invoke-ExternalCommand -Executable $venvPython -Arguments @("-m", "pip", "install", $repoRoot) -Description "Installing the project"

$localDirectory = Join-Path $repoRoot ".local"
New-Item -ItemType Directory -Path $localDirectory -Force | Out-Null
$knowledgeDirectory = Join-Path $localDirectory "knowledge"
$activeKnowledgeDirectory = $null
$existingLocalKnowledgeIsValid = Test-CompleteKnowledgeBundle -Directory $knowledgeDirectory
$requestedExistingKnowledge = $null

if (-not [string]::IsNullOrWhiteSpace($IncludeExistingKnowledge)) {
    if (-not (Test-CompleteKnowledgeBundle -Directory $IncludeExistingKnowledge)) {
        throw "The bundle supplied with -IncludeExistingKnowledge is not a complete, hash-validated manual bundle: $IncludeExistingKnowledge"
    }
    $requestedExistingKnowledge = (Resolve-Path -LiteralPath $IncludeExistingKnowledge).Path
}

if ($SkipKnowledgeDownload) {
    Write-Host "Skipping the official Markdown manual download."
    if ($null -ne $requestedExistingKnowledge) {
        $activeKnowledgeDirectory = $requestedExistingKnowledge
        Write-Host "Using the validated existing bundle supplied with -IncludeExistingKnowledge."
    }
    elseif ($existingLocalKnowledgeIsValid) {
        $activeKnowledgeDirectory = (Resolve-Path -LiteralPath $knowledgeDirectory).Path
        Write-Host "Using the validated existing .local\knowledge bundle."
    }
    else {
        throw "-SkipKnowledgeDownload requires a complete validated bundle. No local bundle was found; rerun setup without this switch or pass -IncludeExistingKnowledge with a valid bundle. No MCP config files were written."
    }
}
else {
    $downloader = Join-Path $repoRoot "scripts\update_knowledge.py"
    $downloadFailure = $null
    if (-not (Test-Path -LiteralPath $downloader -PathType Leaf)) {
        $downloadFailure = "The official manual updater is not present."
    }
    else {
        Write-Host "Downloading and validating the official Markdown manual bundle..."
        try {
            $downloadArguments = @("-B", $downloader, "--output", $knowledgeDirectory)
            $includeExistingArgument = $requestedExistingKnowledge
            if ($null -ne $includeExistingArgument -and $existingLocalKnowledgeIsValid) {
                $resolvedLocalKnowledge = (Resolve-Path -LiteralPath $knowledgeDirectory).Path
                if ($includeExistingArgument.Equals($resolvedLocalKnowledge, [System.StringComparison]::OrdinalIgnoreCase)) {
                    $includeExistingArgument = $null
                }
            }
            if ($null -ne $includeExistingArgument) {
                $downloadArguments += @("--include-existing", $includeExistingArgument)
            }
            Invoke-ExternalCommand -Executable $venvPython -Arguments $downloadArguments -Description "Downloading official manual knowledge"
            if (Test-CompleteKnowledgeBundle -Directory $knowledgeDirectory) {
                $activeKnowledgeDirectory = (Resolve-Path -LiteralPath $knowledgeDirectory).Path
                Write-Host "Validated official manual bundle is ready at .local\knowledge."
            }
            else {
                $downloadFailure = "The updater returned success, but the bundle manifest or referenced manual files did not validate."
            }
        }
        catch {
            $downloadFailure = "Official manual download failed: $($_.Exception.Message)"
        }
    }

    if ($null -eq $activeKnowledgeDirectory) {
        if (Test-CompleteKnowledgeBundle -Directory $knowledgeDirectory) {
            $activeKnowledgeDirectory = (Resolve-Path -LiteralPath $knowledgeDirectory).Path
            Write-Warning "The official update did not complete. Using the previously validated .local\knowledge bundle; it may not include current documentation."
        }
        elseif ($null -ne $requestedExistingKnowledge -and (Test-CompleteKnowledgeBundle -Directory $requestedExistingKnowledge)) {
            $activeKnowledgeDirectory = $requestedExistingKnowledge
            Write-Warning "The official update did not complete. Using the validated bundle supplied with -IncludeExistingKnowledge; it may not include current documentation."
        }
        else {
            $retryCommand = ".\.venv\Scripts\python.exe -B scripts\update_knowledge.py --output `"$knowledgeDirectory`""
            throw "$downloadFailure No complete validated manual bundle is available. Setup stopped without writing MCP config files. Retry with: $retryCommand"
        }
    }
}

$tomlCommand = ConvertTo-TomlString -Value $venvPython
$tomlCwd = ConvertTo-TomlString -Value $repoRoot
$serverEnvironment = @{ PYTHONDONTWRITEBYTECODE = "1" }
$tomlEnvironment = 'env = { PYTHONDONTWRITEBYTECODE = "1"'
if ($null -ne $activeKnowledgeDirectory) {
    $serverEnvironment.IGOR_KNOWLEDGE_DIR = $activeKnowledgeDirectory
    $tomlKnowledgeDirectory = ConvertTo-TomlString -Value $activeKnowledgeDirectory
    $tomlEnvironment += ", IGOR_KNOWLEDGE_DIR = $tomlKnowledgeDirectory"
}
$tomlEnvironment += " }"
$codexConfig = @(
    "[mcp_servers.igor-pro]",
    "command = $tomlCommand",
    'args = ["-B", "-m", "igor_mcp_server"]',
    "cwd = $tomlCwd",
    $tomlEnvironment,
    ""
) -join "`r`n"

$genericConfig = @{
    mcpServers = @{
        "igor-pro" = @{
            command = $venvPython
            args = @("-B", "-m", "igor_mcp_server")
            cwd = $repoRoot
            env = $serverEnvironment
        }
    }
} | ConvertTo-Json -Depth 6

$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$codexPath = Join-Path $localDirectory "mcp-config.codex.toml"
$genericPath = Join-Path $localDirectory "mcp-config.example.json"
[System.IO.File]::WriteAllText($codexPath, $codexConfig, $utf8NoBom)
[System.IO.File]::WriteAllText($genericPath, $genericConfig + "`r`n", $utf8NoBom)

Write-Host ""
Write-Host "Setup complete. The local MCP configuration examples are:"
Write-Host "  Codex TOML: $codexPath"
Write-Host "  Generic JSON: $genericPath"
Write-Host "Copy the matching server entry into your MCP host configuration; setup did not edit host settings."
Write-Host "After reconnecting the host, call ensure_igor when you want the server to connect to Igor Pro."
Write-Host "This script did not start Igor Pro."
