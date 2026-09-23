# Wrapper for the scheduled task: runs predict.py and appends its output to a log file,
# since a scheduled task's console output otherwise goes nowhere you can check later.

$root = "F:\kinjazcodes\predictsys"
$logFile = Join-Path $root "data\predict_log.txt"
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

"`n=== Run at $timestamp ===" | Out-File -FilePath $logFile -Append -Encoding utf8
& "$root\.venv\Scripts\python.exe" "$root\src\predict.py" | Out-File -FilePath $logFile -Append -Encoding utf8
