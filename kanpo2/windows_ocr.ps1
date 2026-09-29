# Windows PowerShell 5.1. Input and output are UTF-8 JSON; document paths are data.
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ProgressPreference = 'SilentlyContinue'
$page = $null
$stream = $null
$bitmap = $null
$dataReader = $null
$outputStream = $null
$exitCode = 0

function Await-Operation($operation, [Type]$resultType) {
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and
        $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    $task = $method.MakeGenericMethod($resultType).Invoke($null, @($operation))
    return $task.GetAwaiter().GetResult()
}

function Await-Action($action) {
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and -not $_.IsGenericMethodDefinition -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction'
    } | Select-Object -First 1
    $task = $method.Invoke($null, @($action))
    $task.GetAwaiter().GetResult() | Out-Null
}

function Read-OcrView($decoder, $engine, [int]$rotation, $region) {
    $viewBitmap = $null
    try {
        $transform = New-Object Windows.Graphics.Imaging.BitmapTransform
        switch ($rotation) {
            0 { $transform.Rotation = [Windows.Graphics.Imaging.BitmapRotation]::None }
            90 { $transform.Rotation = [Windows.Graphics.Imaging.BitmapRotation]::Clockwise90Degrees }
            180 { $transform.Rotation = [Windows.Graphics.Imaging.BitmapRotation]::Clockwise180Degrees }
            270 { $transform.Rotation = [Windows.Graphics.Imaging.BitmapRotation]::Clockwise270Degrees }
            default { throw 'Unsupported OCR rotation.' }
        }
        $viewBitmap = Await-Operation ($decoder.GetSoftwareBitmapAsync([Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
            [Windows.Graphics.Imaging.BitmapAlphaMode]::Ignore, $transform,
            [Windows.Graphics.Imaging.ExifOrientationMode]::IgnoreExifOrientation,
            [Windows.Graphics.Imaging.ColorManagementMode]::DoNotColorManage)) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $recognized = Await-Operation ($engine.RecognizeAsync($viewBitmap)) ([Windows.Media.Ocr.OcrResult])
        $lines = @($recognized.Lines | ForEach-Object {
            @{text=$_.Text; words=@($_.Words | ForEach-Object {
                $bounds = $_.BoundingRect
                @{text=$_.Text; bounds=@{x=[double]$bounds.X; y=[double]$bounds.Y;
                    right=[double]($bounds.X + $bounds.Width); bottom=[double]($bounds.Y + $bounds.Height)}}
            })}
        })
        return @{region=@($region); rotation=$rotation; source_width=[int]$decoder.PixelWidth;
            source_height=[int]$decoder.PixelHeight; width=[int]$viewBitmap.PixelWidth;
            height=[int]$viewBitmap.PixelHeight; text=(@($recognized.Lines | ForEach-Object {$_.Text}) -join "`n"); lines=$lines}
    } finally {
        if ($null -ne $viewBitmap) { ([System.IDisposable]$viewBitmap).Dispose() }
    }
}

try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
    [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
    [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime] | Out-Null
    [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime] | Out-Null
    [Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType=WindowsRuntime] | Out-Null
    [Windows.Data.Pdf.PdfPageRenderOptions, Windows.Data.Pdf, ContentType=WindowsRuntime] | Out-Null
    [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime] | Out-Null
    [Windows.Storage.Streams.DataReader, Windows.Storage.Streams, ContentType=WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapPixelFormat, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapAlphaMode, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapTransform, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapRotation, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.ExifOrientationMode, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.ColorManagementMode, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
    $languages = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag })
    $japanese = New-Object Windows.Globalization.Language('ja')
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($japanese)
    if ($request.mode -eq 'availability') {
        $response = @{ok=$true; available=($null -ne $engine); languages=$languages;
            language='ja'; max_image_dimension=[Windows.Media.Ocr.OcrEngine]::MaxImageDimension;
            message=$(if ($null -ne $engine) { 'Japanese OCR is available.' } else { 'Install the approved Japanese OCR language capability in Windows settings.' })}
    } elseif ($request.mode -in @('page', 'layout', 'render')) {
        if ($request.mode -ne 'render' -and $null -eq $engine) { throw 'Japanese OCR language capability is not installed in Windows.' }
        if (-not ($request.path -is [string]) -or -not [System.IO.Path]::IsPathRooted($request.path) -or
            [System.IO.Path]::GetExtension($request.path) -ine '.pdf') { throw 'An absolute PDF file path is required.' }
        if ($request.page -isnot [int] -and $request.page -isnot [long]) { throw 'An integer page number is required.' }
        if ($request.page -lt 1) { throw 'The page number must be at least 1.' }
        $path = [System.IO.Path]::GetFullPath($request.path)
        if (-not [System.IO.File]::Exists($path)) { throw 'The PDF file does not exist.' }
        $file = Await-Operation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
        $document = Await-Operation ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument])
        if ($request.page -gt $document.PageCount) { throw 'The page number exceeds the PDF page count.' }
        $page = $document.GetPage([uint32]($request.page - 1))
        $size = $page.Size
        if ($size.Width -le 0 -or $size.Height -le 0) { throw 'The PDF page dimensions are invalid.' }
        # Render at 300 DPI unless the OCR engine or memory limit requires less.
        $limit = [Math]::Min(5000, [Windows.Media.Ocr.OcrEngine]::MaxImageDimension)
        $scale = [Math]::Min(300.0 / 96.0, $limit / [Math]::Max($size.Width, $size.Height))
        if ($request.mode -eq 'render') {
            $scale = [Math]::Min(150.0 / 96.0, 1800.0 / [Math]::Max($size.Width, $size.Height))
        }
        if ($request.mode -eq 'layout') {
            if (@($request.regions).Count -lt 1 -or @($request.regions).Count -gt 12 -or
                @($request.rotations).Count -lt 1 -or @($request.rotations).Count -gt 4) { throw 'Invalid OCR view count.' }
            $views = New-Object 'System.Collections.Generic.List[object]'
            foreach ($region in $request.regions) {
                if (@($region).Count -ne 4) { throw 'An OCR region requires x, y, width and height.' }
                $rx=[double]$region[0]; $ry=[double]$region[1]; $rw=[double]$region[2]; $rh=[double]$region[3]
                if ($rx -lt 0 -or $ry -lt 0 -or $rw -le 0 -or $rh -le 0 -or $rx+$rw -gt 1 -or $ry+$rh -gt 1) { throw 'The OCR region is outside the page.' }
                $options = New-Object Windows.Data.Pdf.PdfPageRenderOptions
                $rect = $options.SourceRect
                $rect.X=$size.Width*$rx; $rect.Y=$size.Height*$ry
                $rect.Width=$size.Width*$rw; $rect.Height=$size.Height*$rh
                $options.SourceRect=$rect
                $options.DestinationWidth=[uint32][Math]::Max(1, [Math]::Floor($size.Width*$rw*$scale))
                $options.DestinationHeight=[uint32][Math]::Max(1, [Math]::Floor($size.Height*$rh*$scale))
                $stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
                try {
                    Await-Action ($page.RenderToStreamAsync($stream, $options))
                    $stream.Seek(0)
                    $decoder = Await-Operation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
                    foreach ($angle in $request.rotations) {
                        $views.Add((Read-OcrView $decoder $engine ([int]$angle) $region))
                    }
                } finally {
                    ([System.IDisposable]$stream).Dispose()
                    $stream = $null
                }
            }
            $response = @{ok=$true; page=[int]$request.page; language='ja'; views=@($views.ToArray())}
        } else {
            $options = New-Object Windows.Data.Pdf.PdfPageRenderOptions
            $options.DestinationWidth = [uint32][Math]::Max(1, [Math]::Floor($size.Width * $scale))
            $options.DestinationHeight = [uint32][Math]::Max(1, [Math]::Floor($size.Height * $scale))
            $stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
            Await-Action ($page.RenderToStreamAsync($stream, $options))
            $stream.Seek(0)
            $decoder = Await-Operation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
            if ($request.mode -eq 'render') {
                if (-not ($request.output -is [string]) -or -not [System.IO.Path]::IsPathRooted($request.output) -or
                    [System.IO.Path]::GetExtension($request.output) -ine '.png') { throw 'An absolute PNG output path is required.' }
                $outputPath = [System.IO.Path]::GetFullPath($request.output)
                if ($stream.Size -gt 67108864) { throw 'The preview image is too large.' }
                $dataReader = New-Object Windows.Storage.Streams.DataReader($stream.GetInputStreamAt(0))
                $loaded = Await-Operation ($dataReader.LoadAsync([uint32]$stream.Size)) ([uint32])
                if ($loaded -ne $stream.Size) { throw 'The preview image could not be read completely.' }
                $bytes = New-Object byte[] ([int]$loaded)
                $dataReader.ReadBytes($bytes)
                $outputStream = [System.IO.File]::Open($outputPath, [System.IO.FileMode]::CreateNew,
                    [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
                $outputStream.Write($bytes, 0, $bytes.Length)
                $outputStream.Dispose()
                $outputStream = $null
                $response = @{ok=$true; output=$outputPath; page=[int]$request.page;
                    width=[int]$decoder.PixelWidth; height=[int]$decoder.PixelHeight}
            } else {
                $bitmap = Await-Operation ($decoder.GetSoftwareBitmapAsync([Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8, [Windows.Graphics.Imaging.BitmapAlphaMode]::Ignore)) ([Windows.Graphics.Imaging.SoftwareBitmap])
                $recognized = Await-Operation ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
                $lines = @($recognized.Lines | ForEach-Object { $_.Text })
                $response = @{ok=$true; text=($lines -join "`n"); language='ja'; page=[int]$request.page;
                    width=[int]$decoder.PixelWidth; height=[int]$decoder.PixelHeight}
            }
        }
    } else {
        throw 'Unknown OCR request mode.'
    }
} catch {
    $response = @{ok=$false; error=$_.Exception.Message}
    $exitCode = 1
} finally {
    # WinRT objects expose IClosable; Dispose may need an explicit IDisposable cast.
    foreach ($resource in @($outputStream, $dataReader, $bitmap, $page, $stream)) {
        if ($null -ne $resource) {
            try { ([System.IDisposable]$resource).Dispose() } catch { }
        }
    }
}
[Console]::Out.WriteLine(($response | ConvertTo-Json -Compress -Depth 10))
exit $exitCode
