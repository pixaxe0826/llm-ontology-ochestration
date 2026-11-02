$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$result = @{ toast_submitted = $false; sound_requested = $false; status = 'failed'; history_present = $null }
try {
    $payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $targetUri = [Uri]$payload.url
    if ($targetUri.Scheme -ne 'http' -or $targetUri.Host -ne '127.0.0.1' -or $targetUri.AbsolutePath -ne '/') { throw 'Invalid local notification target' }
    if ($payload.toast) {
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
        [Windows.UI.Notifications.ToastNotifier, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
        [Windows.UI.Notifications.NotificationSetting, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
        [Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
        [Windows.UI.Notifications.ToastNotificationHistory, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
        [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null
        # The existing inbox PowerShell shortcut supplies its registered desktop AppUserModelID.
        $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
        $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId)
        $setting = $notifier.Setting
        if ($null -ne $setting) { $result.status = $setting.ToString() }
        if ($null -eq $setting -or [int]$setting -eq 0) {
            $title = [Security.SecurityElement]::Escape([string]$payload.title)
            $body = [Security.SecurityElement]::Escape([string]$payload.body)
            $launch = [Security.SecurityElement]::Escape([string]$payload.url)
            $audio = '<audio silent="true"/>'
            if ($payload.sound) { $audio = '<audio src="ms-winsoundevent:Notification.Default"/>' }
            $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
            $xml.LoadXml("<toast activationType=`"protocol`" launch=`"$launch`"><visual><binding template=`"ToastGeneric`"><text>$title</text><text>$body</text></binding></visual>$audio</toast>")
            $notification = [Windows.UI.Notifications.ToastNotification]::new($xml)
            $notification.Tag = ([string]$payload.tag).Substring(0, [Math]::Min(16, ([string]$payload.tag).Length))
            $notification.Group = 'MusePlatform'
            $notifier.Show($notification)
            $result.toast_submitted = $true
            $result.sound_requested = [bool]$payload.sound
            $result.status = 'submitted'
            try {
                $result.history_present = @([Windows.UI.Notifications.ToastNotificationManager]::History.GetHistory($appId) | Where-Object { $_.Tag -eq $notification.Tag -and $_.Group -eq 'MusePlatform' }).Count -gt 0
            } catch { }
        }
    } elseif ($payload.sound) {
        [System.Media.SystemSounds]::Exclamation.Play()
        Start-Sleep -Milliseconds 250
        $result.sound_requested = $true
        $result.status = 'sound_requested'
    }
} catch { }
[Console]::Out.Write(($result | ConvertTo-Json -Compress))
