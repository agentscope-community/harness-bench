"""Container-directed tar transfers; optional bounded ACS Python transport."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path
import shlex
import tarfile
import uuid

from harbor.environments.base import transfer_tar_filter

CHUNK_BYTES = 24576
BATCH_SIZE = 4


async def transfer_batch(calls):
    """Drain ALL concurrent requests before releasing a temporary archive."""
    results = await asyncio.gather(*calls, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results


class TransferMixin:
    """Host implements exec and _stream_transfer with ContextVar routing.

    stream uses binary WebSocket tar (no Python required). chunked requires
    python3, and Python >=3.12 for safe remote tar extraction.
    """

    async def _transfer_python(self, code, *arguments):
        command = "python3 -c " + shlex.quote(code) + " " + " ".join(shlex.quote(str(arg)) for arg in arguments)
        result = await self.exec(command, cwd="/", timeout_sec=600, user="root")
        if result.return_code != 0:
            raise RuntimeError(f"Artifact transfer failed ({result.return_code}): {result.stderr or result.stdout}")
        return result.stdout or ""

    async def _download_archive(self, source: str, directory: bool) -> bytes:
        if self.transfer == "stream":
            path = Path(source)
            command = ["tar", "cf", "-", "-C", str(path) if directory else str(path.parent),
                       "." if directory else path.name]
            return await self._stream_transfer(command)
        remote = "/tmp/hbench-transfer-" + uuid.uuid4().hex + ".tgz"
        try:
            metadata = json.loads(await self._transfer_python(
                "import sys,tarfile,pathlib,hashlib,json; p=pathlib.Path(sys.argv[1]); "
                "a=tarfile.open(sys.argv[2],'w:gz'); a.add(p,arcname='.' if sys.argv[3]=='dir' else p.name); a.close(); "
                "b=pathlib.Path(sys.argv[2]).read_bytes(); print(json.dumps({'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()}))",
                source, remote, "dir" if directory else "file"))
            size = metadata["bytes"]
            if not isinstance(size, int) or size < 0:
                raise RuntimeError("Invalid artifact archive size")
            data = bytearray()
            for start in range(0, size, BATCH_SIZE * CHUNK_BYTES):
                offsets = range(start, min(start + BATCH_SIZE * CHUNK_BYTES, size), CHUNK_BYTES)
                values = await transfer_batch([self._transfer_python(
                    "import sys,base64; f=open(sys.argv[1],'rb'); f.seek(int(sys.argv[2])); "
                    f"print(base64.b64encode(f.read({CHUNK_BYTES})).decode())", remote, offset) for offset in offsets])
                for offset, encoded in zip(offsets, values):
                    chunk = base64.b64decode(encoded.strip(), validate=True)
                    if len(chunk) != min(CHUNK_BYTES, size - offset):
                        raise RuntimeError("Truncated artifact chunk")
                    data.extend(chunk)
            if hashlib.sha256(data).hexdigest() != metadata["sha256"]:
                raise RuntimeError("Artifact checksum mismatch")
            return bytes(data)
        finally:
            await self._transfer_python("import pathlib,sys; pathlib.Path(sys.argv[1]).unlink(missing_ok=True)", remote)

    async def _upload_archive(self, data: bytes, target: str):
        if self.transfer == "stream":
            result = await self.exec("mkdir -p " + shlex.quote(target), cwd="/", user="root", timeout_sec=300)
            if result.return_code:
                raise RuntimeError("Unable to create transfer destination: " + str(result.stderr))
            await self._stream_transfer(["tar", "xf", "-", "-C", target], data)
            return
        remote = "/tmp/hbench-transfer-" + uuid.uuid4().hex + ".tgz"
        try:
            await self._transfer_python("import sys; f=open(sys.argv[1],'wb'); f.truncate(int(sys.argv[2])); f.close()", remote, len(data))
            for start in range(0, len(data), BATCH_SIZE * CHUNK_BYTES):
                await transfer_batch([self._transfer_python(
                    "import sys,base64; f=open(sys.argv[1],'r+b'); f.seek(int(sys.argv[2])); "
                    "b=base64.b64decode(sys.argv[3],validate=True); assert f.write(b)==len(b); f.close()",
                    remote, offset, base64.b64encode(data[offset:offset + CHUNK_BYTES]).decode())
                    for offset in range(start, min(start + BATCH_SIZE * CHUNK_BYTES, len(data)), CHUNK_BYTES)])
            await self._transfer_python(
                "import sys,pathlib,hashlib,tarfile; p=pathlib.Path(sys.argv[1]); "
                "assert hashlib.sha256(p.read_bytes()).hexdigest()==sys.argv[3]; "
                "pathlib.Path(sys.argv[2]).mkdir(parents=True,exist_ok=True); "
                "a=tarfile.open(p,'r:gz'); a.extractall(sys.argv[2],filter='data'); a.close()",
                remote, target, hashlib.sha256(data).hexdigest())
        finally:
            await self._transfer_python("import pathlib,sys; pathlib.Path(sys.argv[1]).unlink(missing_ok=True)", remote)

    async def download_dir(self, source_dir, target_dir):
        data = await self._download_archive(str(source_dir), True)
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            archive.extractall(target, filter=transfer_tar_filter)

    async def download_file(self, source_path, target_path):
        data = await self._download_archive(str(source_path), False)
        target = Path(target_path)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            member = archive.getmember(Path(source_path).name)
            if not member.isfile():
                raise ValueError("Expected a regular artifact file")
            payload = archive.extractfile(member).read()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    async def upload_dir(self, source_dir, target_dir):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz" if self.transfer == "chunked" else "w") as archive:
            archive.add(Path(source_dir), arcname=".")
        await self._upload_archive(buffer.getvalue(), str(target_dir))

    async def upload_file(self, source_path, target_path):
        buffer = io.BytesIO()
        target = Path(target_path)
        with tarfile.open(fileobj=buffer, mode="w:gz" if self.transfer == "chunked" else "w") as archive:
            archive.add(Path(source_path), arcname=target.name)
        await self._upload_archive(buffer.getvalue(), str(target.parent))
