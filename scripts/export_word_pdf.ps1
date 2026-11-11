param(
    [Parameter(Mandatory = $true)][string]$InputPath,
    [Parameter(Mandatory = $true)][string]$OutputPath,
    [ValidateSet('Word', 'WPS')][string]$Engine = 'Word'
)
$ErrorActionPreference = 'Stop'
$sourcePath = (Resolve-Path -LiteralPath $InputPath).Path
$targetPath = [IO.Path]::GetFullPath($OutputPath)
if ([IO.File]::Exists($targetPath)) { throw 'QA output already exists' }
if ([IO.Path]::GetExtension($sourcePath) -ne '.docx') { throw 'DOCX input required' }
[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($targetPath)) | Out-Null
$wordInstance = $null
$openedDocument = $null
$falseValue = $false
$trueValue = $true
$noSave = 0
$pdfFormat = 17
try {
    $programId = if ($Engine -eq 'WPS') { 'KWPS.Application' } else { 'Word.Application' }
    $wordInstance = New-Object -ComObject $programId
    $wordInstance.Visible = $false
    $wordInstance.DisplayAlerts = 0
    $wordInstance.AutomationSecurity = 3
    # A private Word instance opens the immutable result read-only, without MRU.
    $openedDocument = $wordInstance.Documents.Open([ref]$sourcePath, [ref]$falseValue, [ref]$trueValue, [ref]$falseValue)
    $openedDocument.Fields.Update() | Out-Null
    $openedDocument.Repaginate()
    foreach ($tocItem in $openedDocument.TablesOfContents) { $tocItem.Update() }
    $openedDocument.ExportAsFixedFormat([ref]$targetPath, [ref]$pdfFormat)
    if (-not [IO.File]::Exists($targetPath)) { throw 'Word did not produce PDF' }
} finally {
    if ($null -ne $openedDocument) {
        $openedDocument.Close([ref]$noSave)
        [Runtime.InteropServices.Marshal]::FinalReleaseComObject($openedDocument) | Out-Null
    }
    if ($null -ne $wordInstance) {
        $wordInstance.Quit([ref]$noSave)
        [Runtime.InteropServices.Marshal]::FinalReleaseComObject($wordInstance) | Out-Null
    }
}
