from __future__ import annotations

import abc
import asyncio
import contextlib
import logging
import pathlib
import tempfile
import zipfile
from collections.abc import AsyncIterable, Callable, Iterable, Iterator
from functools import wraps
from typing import IO, Any

import httpx

from ._models import (
    MultiUpload,
    OutputUpload,
    OutputUploadType,
    SingleFilePostUpload,
    SingleFilePutUpload,
    ZipAndHttpPostUpload,
    ZipAndHttpPutUpload,
)

logger = logging.getLogger(__name__)

OUTPUT_UPLOAD_TIMEOUT_SECONDS = 300
MAX_NUMBER_OF_FILES = 1000
MAX_CONCURRENT_UPLOADS = 3


def retry(
    max_retries: int = 3, initial_delay: float = 1, backoff_factor: float = 2, exceptions: type[Exception] = Exception
) -> Callable[..., Any]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            delay = initial_delay
            for i in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    if i == max_retries - 1:
                        logger.debug(f"Got exception {exc} - but max number of retries reached")
                        raise
                    logger.debug(f"Got exception {exc} but will retry because it is {i + 1} attempt")
                    await asyncio.sleep(delay)
                    delay *= backoff_factor

        return wrapper

    return decorator


class OutputUploadFailed(Exception):
    def __init__(self, description: str):
        self.description = description


class OutputUploader(metaclass=abc.ABCMeta):
    """Upload the output directory to JobRequest.OutputUpload"""

    __output_type_map: dict[type[OutputUpload], Callable[[OutputUpload], OutputUploader]] = {}
    _semaphore = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)

    @classmethod
    @abc.abstractmethod
    def handles_output_type(cls) -> type[OutputUpload]: ...

    @abc.abstractmethod
    async def upload(self, directory: pathlib.Path) -> None: ...

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.__output_type_map[cls.handles_output_type()] = lambda upload: cls(upload)  # type: ignore

    def __init__(self) -> None:
        self.max_size_bytes = 2147483648

    @classmethod
    def for_upload_output(cls, upload_output: OutputUpload) -> OutputUploader:
        return cls.__output_type_map[upload_output.__class__](upload_output)


class ZipAndHTTPPostOutputUploader(OutputUploader):
    """Zip the upload the output directory and HTTP POST the zip file to the given URL"""

    def __init__(self, upload_output: ZipAndHttpPostUpload) -> None:
        super().__init__()
        self.upload_output = upload_output

    @classmethod
    def handles_output_type(cls) -> type[OutputUpload]:
        return ZipAndHttpPostUpload

    async def upload(self, directory: pathlib.Path) -> None:
        with zipped_directory(directory, max_size_bytes=self.max_size_bytes) as (file_size, fp):
            async with self._semaphore:
                await upload_post(
                    fp,
                    "output.zip",
                    file_size,
                    self.upload_output.url,
                    content_type="application/zip",
                    form_fields=self.upload_output.form_fields,
                )


class ZipAndHTTPPutOutputUploader(OutputUploader):
    """Zip the upload the output directory and HTTP PUT the zip file to the given URL"""

    def __init__(self, upload_output: ZipAndHttpPutUpload) -> None:
        super().__init__()
        self.upload_output = upload_output

    @classmethod
    def handles_output_type(cls) -> type[OutputUpload]:
        return ZipAndHttpPutUpload

    async def upload(self, directory: pathlib.Path) -> None:
        with zipped_directory(directory, max_size_bytes=self.max_size_bytes) as (file_size, fp):
            async with self._semaphore:
                await upload_put(fp, file_size, self.upload_output.url)


class MultiUploadOutputUploader(OutputUploader):
    """Upload multiple files to the specified URLs"""

    def __init__(self, upload_output: MultiUpload):
        super().__init__()
        self.upload_output = upload_output

    @classmethod
    def handles_output_type(cls) -> type[OutputUpload]:
        return MultiUpload

    async def upload(self, directory: pathlib.Path) -> None:
        single_file_uploads = []
        tasks = []
        for upload in self.upload_output.uploads:
            file_path = directory / upload.relative_path
            if not file_path.exists():
                raise OutputUploadFailed(f"File not found: {file_path}")

            if upload.output_upload_type == OutputUploadType.single_file_post:
                # we run those concurrently but for loop changes slots - we need to bind
                async def _single_post_upload_task(file_path: pathlib.Path, upload: SingleFilePostUpload) -> None:
                    with file_path.open("rb") as fp:
                        await upload_post(
                            fp,
                            file_path.name,
                            file_path.stat().st_size,
                            upload.url,
                            form_fields=upload.form_fields,
                            headers=upload.signed_headers,
                        )

                async with self._semaphore:
                    tasks.append(_single_post_upload_task(file_path, upload))
                single_file_uploads.append(upload.relative_path)
            elif upload.output_upload_type == OutputUploadType.single_file_put:
                # we run those concurrently but for loop changes slots - we need to bind
                async def _single_put_upload_task(file_path: pathlib.Path, upload: SingleFilePutUpload) -> None:
                    with file_path.open("rb") as fp:
                        await upload_put(fp, file_path.stat().st_size, upload.url, headers=upload.signed_headers)

                async with self._semaphore:
                    tasks.append(_single_put_upload_task(file_path, upload))
                single_file_uploads.append(upload.relative_path)
            else:
                raise OutputUploadFailed(f"Unsupported upload type: {upload.output_upload_type}")

        system_output_upload = self.upload_output.system_output
        if system_output_upload:
            if isinstance(system_output_upload, ZipAndHttpPostUpload):
                # we don't need to bind any vars because we don't run it in a loop
                async def _output_post_upload_task(upload: ZipAndHttpPostUpload) -> None:
                    with zipped_directory(
                        directory, exclude=single_file_uploads, max_size_bytes=self.max_size_bytes
                    ) as (
                        file_size,
                        fp,
                    ):
                        await upload_post(
                            fp,
                            "output.zip",
                            file_size,
                            upload.url,
                            content_type="application/zip",
                            form_fields=upload.form_fields,
                        )

                async with self._semaphore:
                    tasks.append(_output_post_upload_task(system_output_upload))
            elif isinstance(system_output_upload, ZipAndHttpPutUpload):
                # we don't need to bind any vars because we don't run it in a loop
                async def _output_put_upload_task(upload: ZipAndHttpPutUpload) -> None:
                    with zipped_directory(
                        directory, exclude=single_file_uploads, max_size_bytes=self.max_size_bytes
                    ) as (
                        file_size,
                        fp,
                    ):
                        await upload_put(
                            fp,
                            file_size,
                            upload.url,
                        )

                async with self._semaphore:
                    tasks.append(_output_put_upload_task(system_output_upload))
            else:
                raise OutputUploadFailed(
                    f"Unsupported system output upload type: {system_output_upload.output_upload_type}"
                )
        await asyncio.gather(*tasks)


async def make_iterator_async(it: Iterable[Any]) -> AsyncIterable[Any]:
    """
    Make an iterator async.

    This is stupid.
    """
    for x in it:
        yield x


@retry(max_retries=3, exceptions=OutputUploadFailed)
async def upload_post(
    fp: IO[bytes],
    file_name: str,
    file_size: int,
    url: str,
    content_type: str = "application/octet-stream",
    form_fields: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> None:
    fp.seek(0)
    async with httpx.AsyncClient() as client:
        form_fields = {
            "Content-Type": content_type,
            **(form_fields or {}),
        }
        files = {"file": (file_name, fp, content_type)}
        headers = {
            "Content-Length": str(file_size),
            **(headers or {}),
        }
        try:
            logger.debug("Upload (POST) file to: %s", url)
            response = await client.post(
                url=url,
                data=form_fields,
                files=files,
                headers=headers,
                timeout=OUTPUT_UPLOAD_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except httpx.HTTPError as ex:
            raise OutputUploadFailed(f"Uploading output failed with http error {ex}")


@retry(max_retries=3, exceptions=OutputUploadFailed)
async def upload_put(fp: IO[bytes], file_size: int, url: str, headers: dict[str, str] | None = None) -> None:
    fp.seek(0)
    async with httpx.AsyncClient() as client:
        headers = {
            "Content-Length": str(file_size),
            **(headers or {}),
        }
        try:
            logger.debug("Upload (PUT) file to: %s", url)
            response = await client.put(
                url=url,
                content=make_iterator_async(fp),
                headers=headers,
                timeout=OUTPUT_UPLOAD_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except httpx.HTTPError as ex:
            raise OutputUploadFailed(f"Uploading output failed with http error {ex}")


@contextlib.contextmanager
def zipped_directory(
    directory: pathlib.Path, exclude: list[str] | None = None, max_size_bytes: int = 2147483648
) -> Iterator[tuple[int, IO[bytes]]]:
    """
    Context manager that creates a temporary zip file with the files from given directory.
    The temporary file is cleared after the context manager exits.

    :param directory: The directory to zip.
    :param exclude: A list of relative paths to exclude from the zip file.
    :param max_size_bytes: Maximum allowed size of the zip file in bytes. Defaults to 2147483648.

    :return: tuple of size and the file object of the zip file
    """
    files = list(directory.glob("**/*"))
    exclude_set = set(exclude) if exclude else set()

    filtered_files = [file for file in files if str(file.relative_to(directory)) not in exclude_set]

    if len(filtered_files) > MAX_NUMBER_OF_FILES:
        raise OutputUploadFailed("Attempting to upload too many files")

    with tempfile.TemporaryFile() as fp:
        with zipfile.ZipFile(fp, mode="w") as zipf:
            for file in filtered_files:
                zipf.write(filename=file, arcname=file.relative_to(directory))

        file_size = fp.tell()
        fp.seek(0)

        if file_size > max_size_bytes:
            raise OutputUploadFailed("Attempting to upload too large file")

        yield file_size, fp
