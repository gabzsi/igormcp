[CmdletBinding()]
param(
    [string]$Python
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

$tomlCommand = ConvertTo-TomlString -Value $venvPython
$tomlCwd = ConvertTo-TomlString -Value $repoRoot
$codexConfig = @(
    "[mcp_servers.igor-pro]",
    "command = $tomlCommand",
    'args = ["-B", "-m", "igor_mcp_server"]',
    "cwd = $tomlCwd",
    'env = { PYTHONDONTWRITEBYTECODE = "1" }',
    ""
) -join "`r`n"

$genericConfig = @{
    mcpServers = @{
        "igor-pro" = @{
            command = $venvPython
            args = @("-B", "-m", "igor_mcp_server")
            cwd = $repoRoot
            env = @{ PYTHONDONTWRITEBYTECODE = "1" }
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
