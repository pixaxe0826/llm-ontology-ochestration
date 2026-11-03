param([Parameter(Mandatory=$true)][ValidatePattern('^[a-z][a-z0-9_]{0,39}$')][string]$Provider)
$ErrorActionPreference = 'Stop'
if ($Provider -in @('meta','baseten','modal','modal_llm')) { throw 'Use the existing secure key setup script for built-in providers.' }
$variable = if ($Provider -eq 'openai') { 'OPENAI_API_KEY' } else { 'MUSE_PROVIDER_' + $Provider.ToUpperInvariant() + '_API_KEY' }
Write-Host 'Enter the provider key locally. It is never printed or written to project files.'
$secure = Read-Host -Prompt ($Provider + ' API key') -AsSecureString
$pointer = [IntPtr]::Zero
try {
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    if ([string]::IsNullOrWhiteSpace($plain)) { throw 'The key cannot be empty.' }
    [Environment]::SetEnvironmentVariable($variable, $plain, 'User')
    Write-Host ('Saved user credential reference: ' + $variable + '. Refresh connection status in the app.')
} finally {
    $plain = $null
    if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    $secure.Dispose()
}
