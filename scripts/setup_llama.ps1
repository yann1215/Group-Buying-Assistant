$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    throw 'Project .venv is missing. Create it and install requirements.txt first.'
}
& $projectPython -m pip install --only-binary=llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu llama-cpp-python==0.3.36
if ($LASTEXITCODE -ne 0) { throw 'llama-cpp-python installation failed.' }
& $projectPython (Join-Path $PSScriptRoot 'download_qwen_gguf.py')
if ($LASTEXITCODE -ne 0) { throw 'Model download failed.' }
& $projectPython -m pip uninstall -y ollama
if ($LASTEXITCODE -ne 0) { throw 'Old Ollama Python client cleanup failed.' }
