import asyncio
import base64
import io
import pathlib
import zipfile
from unittest import mock

import httpx
import pytest

from compute_horde_core.volume import (
    HuggingfaceVolume,
    HuggingfaceVolumeDownloader,
    InlineVolume,
    InlineVolumeDownloader,
    MultiVolume,
    MultiVolumeDownloader,
    SingleFileVolume,
    SingleFileVolumeDownloader,
    VolumeDownloader,
    VolumeDownloadFailed,
    ZipUrlVolume,
    ZipUrlVolumeDownloader,
)


class TestVolumeDownloader:
    @pytest.fixture
    def mock_volume(self):
        class MockVolume:
            pass

        return MockVolume()

    @pytest.fixture
    def mock_downloader(self, mock_volume):
        class MockVolumeDownloader(VolumeDownloader):
            def __init__(self, volume):
                super().__init__()
                self.volume = volume
                self.download_called = False

            @classmethod
            def handles_volume_type(cls):
                return type(mock_volume)

            async def download(self, directory: pathlib.Path):
                self.download_called = True

        return MockVolumeDownloader(mock_volume)

    def test_volume_downloader_registry(self, mock_volume, mock_downloader):
        """Test that volume downloaders are correctly registered."""
        downloader = VolumeDownloader.for_volume(mock_volume)

        assert isinstance(downloader, mock_downloader.__class__)
        assert downloader.max_retries == 3
        assert downloader.max_size_bytes == 2147483648

    def test_volume_download_failed_exception(self):
        """Test the VolumeDownloadFailed exception."""
        exception = VolumeDownloadFailed("Test failure")
        assert exception.description == "Test failure"


class TestHuggingfaceVolumeDownloader:
    @pytest.fixture
    def volume(self):
        return HuggingfaceVolume(
            repo_id="test/repo",
            revision="main",
            repo_type="model",
            allow_patterns=["*.json"],
            relative_path="models",
            token="test_token",
        )

    @pytest.mark.asyncio
    async def test_download(self, volume, tmp_path):
        """Test the synchronous _download method with mocked huggingface_hub."""
        with mock.patch("huggingface_hub.snapshot_download") as mock_download:
            downloader = HuggingfaceVolumeDownloader(volume)
            await downloader.download(tmp_path)

            mock_download.assert_called_once_with(
                repo_id="test/repo",
                repo_type="model",
                revision="main",
                token="test_token",
                local_dir=tmp_path / "models",
                allow_patterns=["*.json"],
            )

    @pytest.mark.asyncio
    async def test_download_error(self, volume, tmp_path):
        """Test error handling in the synchronous _download method."""
        with mock.patch("huggingface_hub.snapshot_download", side_effect=Exception("Test error")):
            downloader = HuggingfaceVolumeDownloader(volume)
            with pytest.raises(VolumeDownloadFailed) as exc_info:
                await downloader.download(tmp_path)

            assert "Test error" in str(exc_info.value)


class TestInlineVolumeDownloader:
    @pytest.fixture
    def zip_content(self):
        """Create a simple ZIP file content for testing."""
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zip_file:
            zip_file.writestr("test.txt", "Test content")

        return base64.b64encode(zip_buffer.getvalue()).decode("utf-8")

    @pytest.fixture
    def volume(self, zip_content):
        return InlineVolume(
            contents=zip_content,
            relative_path="extracted",
        )

    @pytest.mark.asyncio
    async def test_download(self, volume, tmp_path):
        """Test that InlineVolumeDownloader extracts ZIP contents correctly."""
        downloader = InlineVolumeDownloader(volume)
        await downloader.download(tmp_path)

        # Check that the file was extracted
        extracted_file = tmp_path / "extracted" / "test.txt"
        assert extracted_file.exists()
        assert extracted_file.read_text() == "Test content"

    @pytest.mark.asyncio
    async def test_download_no_relative_path(self, zip_content, tmp_path):
        """Test extraction without a relative path."""
        volume = InlineVolume(contents=zip_content)
        downloader = InlineVolumeDownloader(volume)
        await downloader.download(tmp_path)

        # Check that the file was extracted directly to tmp_path
        extracted_file = tmp_path / "test.txt"
        assert extracted_file.exists()
        assert extracted_file.read_text() == "Test content"

    @pytest.mark.asyncio
    async def test_invalid_base64_content(self, tmp_path):
        """Test handling of invalid base64 content."""
        volume = InlineVolume(
            contents="not-valid-base64!",
            relative_path="extracted",
        )
        downloader = InlineVolumeDownloader(volume)

        with pytest.raises(Exception):
            await downloader.download(tmp_path)

    @pytest.mark.asyncio
    async def test_invalid_zip_content(self, tmp_path):
        """Test handling of invalid zip content."""
        # Valid base64 but not a valid zip
        invalid_zip = base64.b64encode(b"not a zip file").decode("utf-8")
        volume = InlineVolume(
            contents=invalid_zip,
            relative_path="extracted",
        )
        downloader = InlineVolumeDownloader(volume)

        with pytest.raises(zipfile.BadZipFile):
            await downloader.download(tmp_path)


class TestSingleFileVolumeDownloader:
    @pytest.fixture
    def volume(self):
        return SingleFileVolume(
            url="https://example.com/file.txt",
            relative_path="data/file.txt",
        )

    @pytest.mark.asyncio
    async def test_download(self, volume, tmp_path, httpx_mock):
        """Test that SingleFileVolumeDownloader downloads the file correctly."""
        # Setup mock response
        httpx_mock.add_response(url="https://example.com/file.txt", status_code=200, content=b"File content")

        downloader = SingleFileVolumeDownloader(volume)
        await downloader.download(tmp_path)

        # Check that the parent directories were created
        assert (tmp_path / "data").exists()

        # Check that file was downloaded and written
        downloaded_file = tmp_path / "data" / "file.txt"
        assert downloaded_file.exists()
        assert downloaded_file.read_bytes() == b"File content"

        # Verify the request was made with the correct URL
        request = httpx_mock.get_request()
        assert request.url == "https://example.com/file.txt"

    @pytest.mark.asyncio
    async def test_download_large_file(self, tmp_path, httpx_mock):
        """Test downloading a large file that exceeds size limit."""
        volume = SingleFileVolume(
            url="https://example.com/large_file.txt",
            relative_path="large_file.txt",
        )

        # Set up a response with Content-Length larger than limit
        httpx_mock.add_response(
            url="https://example.com/large_file.txt",
            headers={"Content-Length": "1500"},  # Larger than our test limit
            status_code=200,
            content=b"x" * 1500,
        )

        downloader = SingleFileVolumeDownloader(volume)
        downloader.max_size_bytes = 1000  # Set a small limit for testing

        with pytest.raises(VolumeDownloadFailed, match="Input volume too large"):
            await downloader.download(tmp_path)

    @pytest.mark.asyncio
    async def test_download_server_error(self, volume, tmp_path, httpx_mock):
        """Test handling of server errors."""
        # Setup mock response with server error
        httpx_mock.add_response(url="https://example.com/file.txt", status_code=500, content=b"Server Error")

        downloader = SingleFileVolumeDownloader(volume)
        downloader.max_retries = 1  # Set retries low for test speed

        # Should eventually fail after retries
        with pytest.raises(httpx.HTTPStatusError):
            await downloader.download(tmp_path)


class TestZipUrlVolumeDownloader:
    @pytest.fixture
    def volume(self):
        return ZipUrlVolume(
            contents="https://example.com/archive.zip",
            relative_path="extracted",
        )

    @pytest.fixture
    def zip_content(self):
        """Create a simple ZIP file content for testing."""
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zip_file:
            zip_file.writestr("test.txt", "Test content")

        return zip_buffer.getvalue()

    @pytest.mark.asyncio
    async def test_download(self, volume, tmp_path, zip_content, httpx_mock):
        """Test that ZipUrlVolumeDownloader downloads and extracts ZIP files correctly."""
        # Mock the HTTP request for the zip file
        httpx_mock.add_response(url="https://example.com/archive.zip", status_code=200, content=zip_content)

        downloader = ZipUrlVolumeDownloader(volume)
        await downloader.download(tmp_path)

        # Check that the file was extracted
        extracted_file = tmp_path / "extracted" / "test.txt"
        assert extracted_file.exists()
        assert extracted_file.read_text() == "Test content"

    @pytest.mark.asyncio
    async def test_download_no_relative_path(self, tmp_path, zip_content, httpx_mock):
        """Test extraction without a relative path."""
        volume = ZipUrlVolume(contents="https://example.com/archive.zip")

        # Mock the HTTP request
        httpx_mock.add_response(url="https://example.com/archive.zip", status_code=200, content=zip_content)

        downloader = ZipUrlVolumeDownloader(volume)
        await downloader.download(tmp_path)

        # Check that the file was extracted directly to tmp_path
        extracted_file = tmp_path / "test.txt"
        assert extracted_file.exists()
        assert extracted_file.read_text() == "Test content"

    @pytest.mark.asyncio
    async def test_download_invalid_zip(self, volume, tmp_path, httpx_mock):
        """Test handling of invalid zip content."""
        # Mock response with invalid zip content
        httpx_mock.add_response(url="https://example.com/archive.zip", status_code=200, content=b"not a zip file")

        downloader = ZipUrlVolumeDownloader(volume)

        with pytest.raises(zipfile.BadZipFile):
            await downloader.download(tmp_path)

    @pytest.mark.asyncio
    async def test_download_large_zip(self, tmp_path, httpx_mock):
        """Test downloading a large zip file that exceeds size limit."""
        volume = ZipUrlVolume(
            contents="https://example.com/large_archive.zip",
            relative_path="extracted",
        )

        # Generate a large response
        large_content = b"x" * 1500  # Larger than our test limit

        # Set up a response with Content-Length larger than limit
        httpx_mock.add_response(
            url="https://example.com/large_archive.zip",
            headers={"Content-Length": "1500"},
            status_code=200,
            content=large_content,
        )

        downloader = ZipUrlVolumeDownloader(volume)
        downloader.max_size_bytes = 1000  # Set a small limit for testing

        with pytest.raises(VolumeDownloadFailed, match="Input volume too large"):
            await downloader.download(tmp_path)


class TestMultiVolumeDownloader:
    @pytest.fixture
    def text_file_content(self):
        return b"Test file content"

    @pytest.fixture
    def zip_content(self):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zip_file:
            zip_file.writestr("test.txt", "Test content in zip")

        return zip_buffer.getvalue()

    @pytest.fixture
    def volumes(self):
        return [
            SingleFileVolume(url="https://example.com/file1.txt", relative_path="file1.txt"),
            SingleFileVolume(url="https://example.com/file2.txt", relative_path="file2.txt"),
        ]

    @pytest.fixture
    def multi_volume(self, volumes):
        return MultiVolume(volumes=volumes)

    @pytest.fixture
    def mixed_volumes(self):
        return [
            SingleFileVolume(url="https://example.com/file1.txt", relative_path="file1.txt"),
            ZipUrlVolume(contents="https://example.com/archive.zip", relative_path="extracted"),
        ]

    @pytest.fixture
    def mixed_multi_volume(self, mixed_volumes):
        return MultiVolume(volumes=mixed_volumes)

    @pytest.fixture
    def setup_test_dir(self, tmp_path, text_file_content):
        # Create test files
        test_file = tmp_path / "test_file.txt"
        test_file.write_bytes(text_file_content)

        # Create a subdirectory with a file
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        subdir_file = subdir / "subdir_file.txt"
        subdir_file.write_bytes(text_file_content)

        return tmp_path

    @pytest.mark.asyncio
    async def test_download(self, multi_volume, tmp_path, httpx_mock, text_file_content):
        """Test that MultiVolumeDownloader downloads multiple files correctly."""
        # Mock HTTP responses for both files
        httpx_mock.add_response(url="https://example.com/file1.txt", status_code=200, content=text_file_content)
        httpx_mock.add_response(url="https://example.com/file2.txt", status_code=200, content=text_file_content)

        downloader = MultiVolumeDownloader(multi_volume)
        await downloader.download(tmp_path)

        # Check that both files were downloaded
        file1 = tmp_path / "file1.txt"
        file2 = tmp_path / "file2.txt"
        assert file1.exists()
        assert file2.exists()
        assert file1.read_bytes() == text_file_content
        assert file2.read_bytes() == text_file_content

    @pytest.mark.asyncio
    async def test_download_mixed_types(self, mixed_multi_volume, tmp_path, httpx_mock, text_file_content, zip_content):
        """Test downloading different volume types within a MultiVolume."""
        # Mock HTTP responses for file and zip
        httpx_mock.add_response(url="https://example.com/file1.txt", status_code=200, content=text_file_content)
        httpx_mock.add_response(url="https://example.com/archive.zip", status_code=200, content=zip_content)

        downloader = MultiVolumeDownloader(mixed_multi_volume)
        await downloader.download(tmp_path)

        # Check that both the file and zip contents were downloaded and extracted
        file1 = tmp_path / "file1.txt"
        extracted_file = tmp_path / "extracted" / "test.txt"

        assert file1.exists()
        assert extracted_file.exists()
        assert file1.read_bytes() == text_file_content
        assert extracted_file.read_text() == "Test content in zip"

    @pytest.mark.asyncio
    async def test_download_partial_failure(self, multi_volume, tmp_path, httpx_mock, text_file_content):
        """Test behavior when one download fails but others succeed."""
        # First file succeeds
        httpx_mock.add_response(url="https://example.com/file1.txt", status_code=200, content=text_file_content)

        # Second file fails
        httpx_mock.add_response(url="https://example.com/file2.txt", status_code=500, content=b"Server Error")

        downloader = MultiVolumeDownloader(multi_volume)
        downloader.max_retries = 1  # Set retries low for test speed

        # The whole download should fail if any subvolume fails
        with pytest.raises(httpx.HTTPStatusError):
            await downloader.download(tmp_path)

        # First file should still have been downloaded
        file1 = tmp_path / "file1.txt"
        assert file1.exists()
        assert file1.read_bytes() == text_file_content

    @pytest.mark.asyncio
    async def test_concurrency_limit(self, multi_volume, setup_test_dir, httpx_mock):
        """Test that concurrency limit is respected."""
        # Create many download to test concurrency
        many_downloads = []
        for i in range(10):
            # Create files
            file_path = setup_test_dir / f"file_{i}.txt"
            file_path.write_text(f"Content {i}")

            # Create download for each file
            many_downloads.append(
                SingleFileVolume(
                    url=f"https://example.com/download/{i}",
                    relative_path=f"file_{i}.txt",
                )
            )

        # Replace downloads in the fixture
        multi_volume.volumes = many_downloads

        # Mock responses for all URLs
        for i in range(10):
            httpx_mock.add_response(url=f"https://example.com/download/{i}", method="GET", status_code=200)

        # Mock the semaphore to verify it's used correctly
        original_semaphore = asyncio.Semaphore
        semaphore_acquire_count = 0

        class MockSemaphore:
            def __init__(self, value):
                self.sem = original_semaphore(value)
                self.value = value

            async def __aenter__(self):
                nonlocal semaphore_acquire_count
                semaphore_acquire_count += 1
                return await self.sem.__aenter__()

            async def __aexit__(self, *args):
                return await self.sem.__aexit__(*args)

        # Patch the semaphore with our mock
        with mock.patch("compute_horde_core.volume.VolumeDownloader._semaphore", MockSemaphore(3)):
            downloader = MultiVolumeDownloader(multi_volume)
            await downloader.download(setup_test_dir)

        # Verify all requests were made
        assert len(httpx_mock.get_requests()) == 10

        # Verify semaphore was used for each download
        assert semaphore_acquire_count == 10

    @pytest.mark.asyncio
    async def test_empty_volumes(self, tmp_path):
        """Test downloading empty volumes list."""
        empty_multi_volume = MultiVolume(volumes=[])
        downloader = MultiVolumeDownloader(empty_multi_volume)

        # Should complete without errors
        await downloader.download(tmp_path)

        # No files should have been created
        assert len(list(tmp_path.iterdir())) == 0
