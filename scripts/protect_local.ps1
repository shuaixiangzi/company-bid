$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$privateRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot '.local'))
if (-not $privateRoot.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid private root' }
[IO.Directory]::CreateDirectory($privateRoot) | Out-Null
$acl = New-Object System.Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true, $false)
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$sidValues = @($currentSid, [Security.Principal.SecurityIdentifier]'S-1-5-18', [Security.Principal.SecurityIdentifier]'S-1-5-32-544')
foreach ($sidValue in $sidValues) {
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sidValue, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $privateRoot -AclObject $acl
Write-Output 'Private local artifacts restricted to the current account, SYSTEM and administrators.'
