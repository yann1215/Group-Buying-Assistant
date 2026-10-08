"""下载固定版本的官方 Qwen3 GGUF，支持续传并验证 SHA256。"""
import hashlib
import time
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
REVISION = "90862c4b9d2787eaed51d12237eafdfe7c5f6077"
MODEL = "Qwen3-1.7B-Q8_0.gguf"
SIZE = 1834426016
SHA256 = "061b54daade076b5d3362dac252678d17da8c68f07560be70818cace6590cb1a"
URL = f"https://huggingface.co/Qwen/Qwen3-1.7B-GGUF/resolve/{REVISION}/{MODEL}"


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def main():
    directory = ROOT / "models"
    directory.mkdir(exist_ok=True)
    destination = directory / MODEL
    if destination.is_file():
        if destination.stat().st_size != SIZE or digest(destination) != SHA256:
            raise ValueError("已有模型文件校验失败，请移开该文件后重试；不会覆盖未知模型。")
        print("Model already verified.", flush=True)
    else:
        partial = destination.with_suffix(".gguf.part")
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > SIZE:
            raise ValueError("续传文件大小异常，请移开 .part 文件后重试。")
        # 小范围并发传输，完成的区段按顺序追加；重启仍可复用已下载区段。
        ranges = []
        start = offset
        block = 32 * 1024 * 1024
        while start < SIZE:
            end = min((start // block + 1) * block - 1, SIZE - 1)
            ranges.append((start, end))
            start = end + 1

        def fetch(bounds):
            start, end = bounds
            segment = partial.with_name(partial.name + f".{start}-{end}")
            if segment.is_file() and segment.stat().st_size == end - start + 1:
                return segment
            for attempt in range(4):
                try:
                    with requests.get(URL, headers={"Range": f"bytes={start}-{end}"},
                                      stream=True, timeout=(30, 60)) as response:
                        response.raise_for_status()
                        if response.status_code != 206 or response.headers.get("Content-Range", "").split("/")[0] != f"bytes {start}-{end}":
                            raise ValueError("服务器未提供正确的分段下载范围。")
                        with segment.open("wb") as stream:
                            for chunk in response.iter_content(chunk_size=1024 * 1024):
                                stream.write(chunk)
                    if segment.stat().st_size != end - start + 1:
                        raise requests.ConnectionError("Incomplete download segment")
                    return segment
                except requests.RequestException:
                    if attempt == 3:
                        raise
                    time.sleep(2)

        with ThreadPoolExecutor(max_workers=4) as executor:
            for segment in executor.map(fetch, ranges):
                with partial.open("ab") as target, segment.open("rb") as source:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                # 精确的已验证工作区文件；不用递归清理模型目录。
                if segment.resolve().parent != directory.resolve():
                    raise ValueError("下载区段超出模型目录。")
                segment.unlink()
                print(f"Downloaded {partial.stat().st_size / SIZE:.0%}", flush=True)
        if digest(partial) != SHA256:
            raise ValueError("模型 SHA256 校验失败，保留 .part 文件供排查。")
        partial.replace(destination)
        print(f"Model verified: {destination}", flush=True)
    license_path = directory / "LICENSE-Qwen.txt"
    if not license_path.exists():
        response = requests.get(f"https://huggingface.co/Qwen/Qwen3-1.7B-GGUF/resolve/{REVISION}/LICENSE", timeout=30)
        response.raise_for_status()
        license_path.write_text(response.text, encoding="utf-8")


if __name__ == "__main__":
    main()
