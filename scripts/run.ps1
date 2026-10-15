<#
.SYNOPSIS
Run the existing Pulse Agent using the project-local Python environment.
.EXAMPLE
.\scripts\run.ps1 -Action Check
.EXAMPLE
.\scripts\run.ps1 -Action Preview -SnapshotId <snapshot-uuid> -DryRun
#>
[CmdletBinding()]
param(
    [ValidateSet('Help', 'Check', 'Analyze', 'Preview', 'Publish', 'Inspect', 'Resume', 'ConfirmLayout', 'Test', 'Lint')]
    [string]$Action = 'Help',
    [string]$Config = 'config/settings.groq.example.yaml',
    [string]$SnapshotId,
    [string]$RunId,
    [string]$McpConfig = 'config/mcp_server.local.yaml',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'

if ($Action -eq 'Help') {
    @'
Pulse Agent scripts (run from any working directory)

  run.ps1 -Action Check
  run.ps1 -Action Preview -SnapshotId <uuid> [-Config <yaml>]
  run.ps1 -Action Analyze -SnapshotId <uuid> [-Config <yaml>]
  run.ps1 -Action Inspect -RunId <uuid>
  run.ps1 -Action Resume -RunId <uuid> -Config <original-run-config>
  run.ps1 -Action Publish -SnapshotId <uuid> [-McpConfig <yaml>]
  run.ps1 -Action ConfirmLayout -RunId <uuid>
  run.ps1 -Action Test
  run.ps1 -Action Lint

Add -DryRun to display arguments without executing them.
Paths are relative to the project root unless absolute.
Preview/Analyze/Publish and an unfinished Resume can make billable model calls.
Publish creates a Doc and an unsent draft. ConfirmLayout records YOUR one-page check.
No action advances to Phase 6 or schedules future runs.
'@ | Write-Output
    exit 0
}

$exitCode = 1
Push-Location -LiteralPath $projectRoot
try {
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        throw 'Project Python is missing. Follow the setup instructions in README.md.'
    }
    if ($Action -in @('Analyze', 'Preview', 'Publish')) {
        $parsedId = [guid]::Empty
        if (-not [guid]::TryParse($SnapshotId, [ref]$parsedId)) {
            throw 'Supply a valid -SnapshotId. Choose the reporting period explicitly.'
        }
    } elseif ($PSBoundParameters.ContainsKey('SnapshotId')) {
        throw '-SnapshotId applies only to Analyze, Preview, or Publish.'
    }
    if ($Action -in @('Inspect', 'Resume', 'ConfirmLayout')) {
        $parsedId = [guid]::Empty
        if (-not [guid]::TryParse($RunId, [ref]$parsedId)) {
            throw 'Supply a valid -RunId.'
        }
    } elseif ($PSBoundParameters.ContainsKey('RunId')) {
        throw '-RunId applies only to Inspect, Resume, or ConfirmLayout.'
    }

    if ($Action -eq 'Test') {
        # A fresh directory avoids cleaning up previous test runs.
        $testTemp = '.cache/script-tests-' + [guid]::NewGuid().ToString('N')
        $pythonArgs = @('-m', 'pytest', '-p', 'no:cacheprovider', '--basetemp', $testTemp)
    } elseif ($Action -eq 'Lint') {
        $pythonArgs = @('-m', 'ruff', 'check', 'src', 'tests')
    } else {
        if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) {
            throw 'Settings file not found. Use -Config with an existing YAML file.'
        }
        $pythonArgs = @('-m', 'groww_pulse', '--config', $Config)
        switch ($Action) {
            'Check' { $pythonArgs += @('config', 'check') }
            'Analyze' { $pythonArgs += @('analyze', '--snapshot-id', $SnapshotId) }
            'Preview' { $pythonArgs += @('preview', '--snapshot-id', $SnapshotId) }
            'Publish' {
                if (-not $DryRun -and -not (Test-Path -LiteralPath $McpConfig -PathType Leaf)) {
                    throw 'MCP configuration is missing. See Docs/input.md section 7.'
                }
                $pythonArgs += @('publish', '--snapshot-id', $SnapshotId, '--mcp-config', $McpConfig)
            }
            'Inspect' { $pythonArgs += @('inspect', $RunId) }
            'Resume' { $pythonArgs += @('resume', $RunId, '--mcp-config', $McpConfig) }
            'ConfirmLayout' { $pythonArgs += @('confirm-layout', $RunId) }
        }
    }

    if ($DryRun) {
        [ordered]@{ directory = $projectRoot; executable = $pythonPath; arguments = $pythonArgs } |
            ConvertTo-Json -Depth 3
        $exitCode = 0
    } else {
        # Array invocation preserves spaces and never evaluates arguments as shell code.
        & $pythonPath @pythonArgs
        $exitCode = $LASTEXITCODE
    }
} catch {
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
    $exitCode = 2
} finally {
    Pop-Location
}
exit $exitCode
