param([ValidateSet('fingerprint', 'recognize')][string]$Mode = 'fingerprint')
# A single OS API operation. Parameters arrive only over private UTF-8 stdin.
# Stdout is exactly one JSON object; no source/path/error is written to console.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$stream = $null; $bitmap = $null; $converted = $null
$failureCode = 'MATERIAL_OCR_ENGINE_FAILED'
function Get-LocalSha256([string]$Path) {
    $inputStream = [IO.File]::OpenRead($Path)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($hasher.ComputeHash($inputStream)).Replace('-', '').ToLowerInvariant() }
    finally { $hasher.Dispose(); $inputStream.Dispose() }
}
try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    if ($request.language -ne 'zh-Hans-CN') { $failureCode = 'MATERIAL_OCR_LANGUAGE_UNSUPPORTED'; throw 'Unsupported language' }
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
    [Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime] | Out-Null
    $languages = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag } | Sort-Object -CaseSensitive)
    if ($languages -cnotcontains 'zh-Hans-CN') { $failureCode = 'MATERIAL_OCR_LANGUAGE_UNAVAILABLE'; throw 'Recognizer unavailable' }
    $system = [Environment]::SystemDirectory
    $ocrDll = Join-Path $system 'Windows.Media.Ocr.dll'
    $powershellExe = Join-Path $system 'WindowsPowerShell\v1.0\powershell.exe'
    $architecture = if ([Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() -eq 'Arm64') { 'arm64' } elseif ([Environment]::Is64BitOperatingSystem) { 'x64' } else { 'x86' }
    $fingerprint = [ordered]@{
        schema_version = 'material.windows-ocr.engine.v1'; adapter_version = 'material-local-ocr.v1'
        engine = 'windows_ocr'; language = 'zh-Hans-CN'; available_languages = $languages
        max_image_dimension = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension
        os = [ordered]@{ version = [Environment]::OSVersion.Version.ToString(); architecture = $architecture }
        powershell = [ordered]@{ version = $PSVersionTable.PSVersion.ToString(); exe_sha256 = (Get-LocalSha256 $powershellExe) }
        ocr_dll = [ordered]@{ file_version = [Diagnostics.FileVersionInfo]::GetVersionInfo($ocrDll).FileVersion; sha256 = (Get-LocalSha256 $ocrDll) }
        script_sha256 = (Get-LocalSha256 $PSCommandPath)
    }
    if ($Mode -eq 'fingerprint') {
        [ordered]@{ status = 'succeeded'; engine_fingerprint = $fingerprint } | ConvertTo-Json -Depth 12 -Compress
        exit 0
    }
    # Compare each frozen component before invoking RecognizeAsync. JSON object
    # property order is not a provenance condition; each value is exact.
    $expected = $request.expected_engine_fingerprint
    foreach ($key in @('schema_version','adapter_version','engine','language','max_image_dimension','script_sha256')) {
        if ($null -eq $expected -or $expected.$key -cne $fingerprint[$key]) { $failureCode = 'MATERIAL_OCR_ENGINE_FINGERPRINT_MISMATCH'; throw 'Frozen engine changed' }
    }
    foreach ($pair in @(@('os','version'),@('os','architecture'),@('powershell','version'),@('powershell','exe_sha256'),@('ocr_dll','file_version'),@('ocr_dll','sha256'))) {
        $section = $pair[0]; $key = $pair[1]
        if ($expected.$section.$key -cne $fingerprint[$section][$key]) { $failureCode = 'MATERIAL_OCR_ENGINE_FINGERPRINT_MISMATCH'; throw 'Frozen engine changed' }
    }
    if (($expected.available_languages -join '|') -cne ($languages -join '|')) { $failureCode = 'MATERIAL_OCR_ENGINE_FINGERPRINT_MISMATCH'; throw 'Frozen languages changed' }
    if (-not [IO.Path]::IsPathRooted($request.image_path) -or $request.image_sha256 -cnotmatch '^[0-9a-f]{64}$') { $failureCode = 'MATERIAL_OCR_IMAGE_PATH_INVALID'; throw 'Invalid image identity' }
    if ([IO.FileInfo]::new($request.image_path).Length -gt 20000000) { $failureCode = 'MATERIAL_OCR_IMAGE_LIMIT_EXCEEDED'; throw 'Image too large' }
    if ((Get-LocalSha256 $request.image_path) -cne $request.image_sha256) { $failureCode = 'MATERIAL_OCR_IMAGE_HASH_MISMATCH'; throw 'Image changed' }
    [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime] | Out-Null
    [Windows.Storage.FileAccessMode,Windows.Storage,ContentType=WindowsRuntime] | Out-Null
    [Windows.Storage.Streams.IRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapPixelFormat,Windows.Graphics.Imaging,ContentType=WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapAlphaMode,Windows.Graphics.Imaging,ContentType=WindowsRuntime] | Out-Null
    [Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetGenericArguments().Count -eq 1 -and
        $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    function Wait-WinRT($operation, [Type]$type) {
        $task = $asTask.MakeGenericMethod($type).Invoke($null, @($operation))
        $task.Wait()
        return $task.Result
    }
    $file = Wait-WinRT ([Windows.Storage.StorageFile]::GetFileFromPathAsync($request.image_path)) ([Windows.Storage.StorageFile])
    $stream = Wait-WinRT ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Wait-WinRT ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    if ($decoder.PixelWidth -ne $request.width -or $decoder.PixelHeight -ne $request.height -or
        $decoder.PixelWidth -lt 1 -or $decoder.PixelHeight -lt 1 -or $decoder.PixelWidth -gt 10000 -or
        $decoder.PixelHeight -gt 10000 -or ([long]$decoder.PixelWidth * $decoder.PixelHeight) -gt 50000000 -or
        $decoder.PixelWidth -gt $fingerprint.max_image_dimension -or $decoder.PixelHeight -gt $fingerprint.max_image_dimension -or
        $decoder.FrameCount -ne 1) { $failureCode = 'MATERIAL_OCR_IMAGE_DIMENSION_MISMATCH'; throw 'Image dimensions changed or unsupported' }
    $bitmap = Wait-WinRT ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $converted = [Windows.Graphics.Imaging.SoftwareBitmap]::Convert($bitmap, [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8, [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied)
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('zh-Hans-CN'))
    if ($null -eq $engine) { $failureCode = 'MATERIAL_OCR_LANGUAGE_UNAVAILABLE'; throw 'Recognizer unavailable' }
    $result = Wait-WinRT ($engine.RecognizeAsync($converted)) ([Windows.Media.Ocr.OcrResult])
    if ((Get-LocalSha256 $request.image_path) -cne $request.image_sha256) { $failureCode = 'MATERIAL_OCR_IMAGE_HASH_MISMATCH'; throw 'Image changed during recognition' }
    $lines = @($result.Lines | ForEach-Object {
        [ordered]@{ text = $_.Text; words = @($_.Words | ForEach-Object {
            [ordered]@{ text = $_.Text; box = @($_.BoundingRect.X, $_.BoundingRect.Y, $_.BoundingRect.Width, $_.BoundingRect.Height) }
        }) }
    })
    [ordered]@{ schema_version = 'material.windows-ocr.response.v1'; status = 'succeeded'; text = $result.Text
        lines = $lines; width = $converted.PixelWidth; height = $converted.PixelHeight
        image_sha256 = $request.image_sha256; engine_fingerprint = $fingerprint } | ConvertTo-Json -Depth 12 -Compress
} catch {
    [ordered]@{ status = 'failed'; error_code = $failureCode; diagnostic = [ordered]@{
        exception = $_.Exception.ToString(); error_record = ($_ | Out-String)
    } } | ConvertTo-Json -Depth 12 -Compress
    exit 1
} finally {
    if ($null -ne $converted) { $converted.Dispose() }
    if ($null -ne $bitmap) { $bitmap.Dispose() }
    if ($null -ne $stream) { $stream.Dispose() }
}
