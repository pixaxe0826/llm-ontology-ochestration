[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$secure = $null
$plain = $null
$pointer = [IntPtr]::Zero
try {
    $secure = Read-Host 'BASETEN_API_KEY (화면에 표시되지 않습니다)' -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    if ([string]::IsNullOrWhiteSpace($plain) -or $plain -match '\s' -or $plain.Length -gt 1024) { throw 'Invalid input' }
    $setxPath = Join-Path $env:SystemRoot 'System32\setx.exe'
    & $setxPath BASETEN_API_KEY $plain *> $null
    if ($LASTEXITCODE -ne 0) { throw 'setx failed' }
    Write-Host 'Baseten 키를 사용자 환경 변수에 저장했습니다. WebUI 설정에서 연결 상태를 새로 고치세요.'
}
catch {
    Write-Host '저장하지 못했습니다. 키 형식·공백·1024자 길이 제한을 확인하세요. 값은 출력하지 않습니다.'
    exit 1
}
finally {
    $plain = $null
    if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    if ($null -ne $secure) { $secure.Dispose() }
}
