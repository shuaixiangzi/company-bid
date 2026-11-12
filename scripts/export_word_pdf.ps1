param(
    [Parameter(Mandatory = $true)][string]$InputPath,
    [Parameter(Mandatory = $true)][string]$OutputPath,
    [ValidateSet('Word', 'WPS')][string]$Engine = 'Word'
)
$ErrorActionPreference = 'Stop'
$sourcePath = (Resolve-Path -LiteralPath $InputPath).Path
$targetPath = [IO.Path]::GetFullPath($OutputPath)
if ([IO.File]::Exists($targetPath)) { throw 'QA output already exists' }
if ([IO.File]::Exists($targetPath + '.com-safety.json')) { throw 'QA safety evidence already exists' }
if ([IO.Path]::GetExtension($sourcePath) -ne '.docx') { throw 'DOCX input required' }
[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($targetPath)) | Out-Null
$wordInstance = $null
$openedDocument = $null
$documents = $null
$falseValue = $false
$trueValue = $true
$noSave = 0
$pdfFormat = 17
$missingValue = [Type]::Missing
$startedAt = [DateTime]::UtcNow
# CoCreateInstance is allowed to return a shared WPS instance. Process and ROT
# observations are deliberately conservative: an existing office process always
# forbids Quit, even when it is not registered in the running object table.
$existingOfficeProcesses = @(Get-Process -Name 'wps', 'winword' -ErrorAction SilentlyContinue)
$existingOfficeProcessIds = @($existingOfficeProcesses | ForEach-Object { $_.Id })
$attachedToRunningObject = $false
$ownedApplicationProcess = $null
$initialDocumentCount = $null
$openedByThisCall = $false
$documentClosedWithoutSaving = $false
$applicationQuit = $false
$exportCompleted = $false
$remainingDocumentCount = $null
$initialDocumentsStillOpen = $null

function Get-QaFileHash([string]$path) {
    $stream = [IO.File]::OpenRead($path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}
$inputHashBefore = Get-QaFileHash $sourcePath

function Get-DocumentIdentities($collection) {
    $identities = @()
    for ($index = 1; $index -le $collection.Count; $index++) {
        $item = $null
        try {
            $item = $collection.Item($index)
            # Do not publish user document names. The paths are used only to
            # reject a document that was already open before this invocation.
            $identities += [string]$item.FullName
        } finally {
            if ($null -ne $item) {
                [Runtime.InteropServices.Marshal]::ReleaseComObject($item) | Out-Null
            }
        }
    }
    return $identities
}

function Get-ComIdentity($value) {
    $pointer = [Runtime.InteropServices.Marshal]::GetIUnknownForObject($value)
    try { return $pointer.ToInt64().ToString('X') }
    finally { [Runtime.InteropServices.Marshal]::Release($pointer) | Out-Null }
}

function Get-DocumentObjectIdentities($collection) {
    $identities = @()
    for ($index = 1; $index -le $collection.Count; $index++) {
        $item = $null
        try {
            $item = $collection.Item($index)
            $identities += Get-ComIdentity $item
        } finally {
            if ($null -ne $item) { [Runtime.InteropServices.Marshal]::ReleaseComObject($item) | Out-Null }
        }
    }
    return $identities
}

try {
    $programId = if ($Engine -eq 'WPS') { 'KWPS.Application' } else { 'Word.Application' }
    try {
        $wordInstance = [Runtime.InteropServices.Marshal]::GetActiveObject($programId)
        $attachedToRunningObject = $true
    } catch [Runtime.InteropServices.COMException] {
        if ($_.Exception.HResult -ne -2147221021) { throw }
        $wordInstance = New-Object -ComObject $programId
    }
    # Never change Application.Visible, DisplayAlerts or AutomationSecurity.
    $documents = $wordInstance.Documents
    $initialDocumentCount = [int]$documents.Count
    $initialDocumentIdentities = @(Get-DocumentIdentities $documents)
    $initialDocumentObjectIdentities = @(Get-DocumentObjectIdentities $documents)
    if (@($initialDocumentIdentities | Where-Object { $_ -ieq $sourcePath }).Count) {
        throw 'QA input is already open; refusing to operate on an existing document'
    }

    # A new process must be positively identified before this helper may Quit.
    # If Hwnd/process ownership cannot be proved, releasing COM is the only
    # application-level cleanup; a possibly shared application stays running.
    if (-not $attachedToRunningObject -and $existingOfficeProcessIds.Count -eq 0 -and $initialDocumentCount -eq 0) {
        try {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class QaOfficeProcess {
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
}
'@
            [uint32]$candidateProcessId = 0
            [QaOfficeProcess]::GetWindowThreadProcessId([IntPtr]$wordInstance.Hwnd, [ref]$candidateProcessId) | Out-Null
            if ($candidateProcessId -ne 0) {
                $candidateProcess = Get-Process -Id $candidateProcessId
                if ($candidateProcess.ProcessName -in @('wps', 'winword') -and $candidateProcess.StartTime.ToUniversalTime() -ge $startedAt) {
                    $ownedApplicationProcess = $candidateProcessId
                }
            }
        } catch {
            $ownedApplicationProcess = $null
        }
    }

    # Open's twelfth parameter is document-local Visible (confirmed against the
    # installed WPS type library). Do not open visibly then hide the application.
    $openedDocument = $documents.Open(
        [ref]$sourcePath, [ref]$falseValue, [ref]$trueValue, [ref]$falseValue,
        [ref]$missingValue, [ref]$missingValue, [ref]$missingValue, [ref]$missingValue,
        [ref]$missingValue, [ref]$missingValue, [ref]$missingValue, [ref]$falseValue,
        [ref]$falseValue, [ref]$missingValue, [ref]$trueValue, [ref]$missingValue)
    # Path aliases must not allow Open to return a pre-existing document and
    # accidentally make it eligible for Close. Check actual COM identity too.
    if ((Get-ComIdentity $openedDocument) -in $initialDocumentObjectIdentities) {
        throw 'Office returned an existing document; refusing to change or close it'
    }
    $openedByThisCall = $true
    if (-not $openedDocument.ReadOnly -or [string]$openedDocument.FullName -ine $sourcePath) {
        throw 'Office did not honor the read-only QA open contract'
    }
    $qaWindows = $null
    try {
        $qaWindows = $openedDocument.Windows
        for ($windowIndex = 1; $windowIndex -le $qaWindows.Count; $windowIndex++) {
            $qaWindow = $null
            try {
                $qaWindow = $qaWindows.Item($windowIndex)
                if ($qaWindow.Visible) { throw 'Office did not honor hidden document opening' }
            } finally {
                if ($null -ne $qaWindow) { [Runtime.InteropServices.Marshal]::ReleaseComObject($qaWindow) | Out-Null }
            }
        }
    } finally {
        if ($null -ne $qaWindows) { [Runtime.InteropServices.Marshal]::ReleaseComObject($qaWindows) | Out-Null }
    }
    $openedDocument.Fields.Update() | Out-Null
    $openedDocument.Repaginate()
    foreach ($tocItem in $openedDocument.TablesOfContents) { $tocItem.Update() }
    $openedDocument.ExportAsFixedFormat([ref]$targetPath, [ref]$pdfFormat)
    if (-not [IO.File]::Exists($targetPath)) { throw 'Word did not produce PDF' }
    $exportCompleted = $true
} finally {
    try {
        if ($openedByThisCall -and $null -ne $openedDocument) {
            $openedDocument.Close([ref]$noSave)
            $documentClosedWithoutSaving = $true
        }
    } finally {
        if ($null -ne $openedDocument) {
            [Runtime.InteropServices.Marshal]::ReleaseComObject($openedDocument) | Out-Null
        }
        if ($null -ne $documents) {
            $remainingDocumentCount = [int]$documents.Count
            $remainingIdentities = @(Get-DocumentIdentities $documents)
            $initialDocumentsStillOpen = @($initialDocumentIdentities | Where-Object { $_ -notin $remainingIdentities }).Count -eq 0
            # There is intentionally no ActiveDocument.Close or process kill.
            # Only an owned, newly started application with no other documents
            # can be shut down. Existing office processes categorically forbid it.
            if ($null -ne $ownedApplicationProcess -and $existingOfficeProcessIds.Count -eq 0 -and
                    $documentClosedWithoutSaving -and $documents.Count -eq 0) {
                $wordInstance.Quit([ref]$noSave)
                $applicationQuit = $true
            }
            [Runtime.InteropServices.Marshal]::ReleaseComObject($documents) | Out-Null
        }
        if ($null -ne $wordInstance) {
            [Runtime.InteropServices.Marshal]::ReleaseComObject($wordInstance) | Out-Null
        }
    }
    if ($exportCompleted) {
        [ordered]@{
            engine = $Engine
            existing_office_process_ids = $existingOfficeProcessIds
            attached_to_running_object = $attachedToRunningObject
            initial_document_count = $initialDocumentCount
            remaining_document_count = $remainingDocumentCount
            initial_documents_still_open = $initialDocumentsStillOpen
            initial_office_processes_still_running = @($existingOfficeProcessIds | Where-Object { $null -eq (Get-Process -Id $_ -ErrorAction SilentlyContinue) }).Count -eq 0
            document_opened_readonly_hidden_without_mru = $true
            document_closed_without_saving = $documentClosedWithoutSaving
            application_global_settings_written = $false
            application_quit = $applicationQuit
            input_sha256_before = $inputHashBefore
            input_sha256_after = Get-QaFileHash $sourcePath
            input_unchanged = $inputHashBefore -eq (Get-QaFileHash $sourcePath)
            pdf_sha256 = Get-QaFileHash $targetPath
        } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath ($targetPath + '.com-safety.json') -Encoding UTF8
    }
}
