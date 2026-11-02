[CmdletBinding()]
param(
    [ValidateSet('All', 'Meta', 'Modal', 'Proxy')]
    [string]$Service = 'All'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:failureMessage = '설정 중 오류가 발생했습니다. 키 값은 표시하지 않습니다.'

function Read-PrivateValue {
    param([string]$Label, [string]$Pattern = '^\S+$')
    $secure = $null
    $pointer = [IntPtr]::Zero
    $plain = $null
    try {
        $secure = Read-Host $Label -AsSecureString
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        $script:failureMessage = "$Label 입력이 비어 있거나 형식이 올바르지 않습니다. 다시 실행하세요."
        if ([string]::IsNullOrWhiteSpace($plain) -or $plain -notmatch $Pattern) { throw 'Invalid input' }
        if ($plain.Length -gt 1024) {
            $script:failureMessage = 'setx의 길이 제한(1024자)을 초과하여 저장하지 않았습니다.'
            throw 'setx length limit exceeded'
        }
        return $plain
    }
    finally {
        $plain = $null
        if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
        if ($null -ne $secure) { $secure.Dispose() }
    }
}

function Save-PrivateValue {
    param([string]$Name, [string]$Value)
    $script:failureMessage = "$Name 저장에 실패했습니다. 이전에 저장된 다른 항목은 유지됩니다."
    $setxPath = Join-Path $env:SystemRoot 'System32\setx.exe'
    & $setxPath $Name $Value *> $null
    if ($LASTEXITCODE -ne 0) { throw 'setx failed' }
    if ([Environment]::GetEnvironmentVariable($Name, 'User') -cne $Value) { throw 'Saved value verification failed' }
    [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
}

$metaPlain = $null
$modalId = $null
$modalSecret = $null
$proxyId = $null
$proxySecret = $null
$proxyCombined = $null
$hadFailure = $false
try {
    Write-Host '키 값은 화면이나 프로젝트 파일에 기록하지 않습니다.'
    Write-Host '현재 Windows 사용자 환경 변수에 setx로 저장합니다.'
    if ($Service -eq 'All' -or $Service -eq 'Meta') {
        Write-Host '1. Meta 키 저장 및 연결 시험'
        $metaPlain = Read-PrivateValue 'Meta MODEL_API_KEY'
        Save-PrivateValue 'MODEL_API_KEY' $metaPlain
        $metaPlain = $null
        Write-Host 'Meta 키 저장 완료. 목록 조회 1회, 짧은 응답 최대 1회를 실행합니다.'
        $projectRoot = Split-Path -Parent $PSScriptRoot
        $pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
        $testPath = Join-Path $projectRoot 'tests\test_meta.py'
        if ((Test-Path -LiteralPath $pythonPath) -and (Test-Path -LiteralPath $testPath)) {
            & $pythonPath -X utf8 $testPath
            if ($LASTEXITCODE -ne 0) {
                $hadFailure = $true
                Write-Host 'Meta 시험은 통과하지 못했습니다. 키는 저장되어 있으며 자동 재시도하지 않습니다.'
            }
        }
        else {
            $hadFailure = $true
            Write-Host 'Meta 시험 NOT RUN: 프로젝트 가상환경 또는 tests\test_meta.py가 없습니다.'
        }
    }
    if ($Service -eq 'All' -or $Service -eq 'Modal') {
        Write-Host '2. Modal SDK/CLI용 API 토큰 입력'
        Write-Host 'ID(ak-...)와 Secret(as-...)을 합치지 말고 각각 입력하세요.'
        $modalId = Read-PrivateValue 'Modal Token ID (ak-...)' '^ak-[A-Za-z0-9_-]+$'
        $modalSecret = Read-PrivateValue 'Modal Token Secret (as-...)' '^as-[A-Za-z0-9_-]+$'
        Save-PrivateValue 'MODAL_TOKEN_ID' $modalId
        Save-PrivateValue 'MODAL_TOKEN_SECRET' $modalSecret
        $modalId = $null
        $modalSecret = $null
        Write-Host 'Modal API 토큰 저장 완료. GPU 실행이나 배포는 하지 않았습니다.'
        Write-Host '추론 엔드포인트용 Proxy Token(wk-/ws-)은 별도입니다. 필요 시 -Service Proxy로 입력하세요.'
    }
    if ($Service -eq 'Proxy') {
        Write-Host 'Modal 추론 엔드포인트용 Proxy Token 입력'
        Write-Host 'API 토큰(ak-/as-) 대신 Proxy Token(wk-/ws-)을 입력하세요.'
        $proxyId = Read-PrivateValue 'Proxy Token ID (wk-...)' '^wk-[A-Za-z0-9_-]+$'
        $proxySecret = Read-PrivateValue 'Proxy Token Secret (ws-...)' '^ws-[A-Za-z0-9_-]+$'
        $proxyCombined = $proxyId + '.' + $proxySecret
        if ($proxyCombined.Length -gt 1024) {
            $script:failureMessage = '합친 Proxy Token이 setx 길이 제한(1024자)을 초과하여 저장하지 않았습니다.'
            throw 'setx length limit exceeded'
        }
        Save-PrivateValue 'MODAL_PROXY_TOKEN' $proxyCombined
        Write-Host 'MODAL_PROXY_TOKEN 저장 완료. 엔드포인트 시험은 실행하지 않았습니다.'
    }
    Write-Host '터미널을 완전히 닫고 다시 여세요. 채팅에는 키 대신 시험 결과와 완료 여부만 알려 주세요.'
}
catch {
    Write-Host $script:failureMessage -ForegroundColor Red
    exit 1
}
finally {
    $metaPlain = $null
    $modalId = $null
    $modalSecret = $null
    $proxyId = $null
    $proxySecret = $null
    $proxyCombined = $null
}
if ($hadFailure) { exit 1 }
