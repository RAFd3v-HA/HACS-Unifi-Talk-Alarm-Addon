"""Bounded TTS and remote WAV preparation for the SIP audio source."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import shutil
import tempfile
from urllib.parse import urlsplit
import wave

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector

from .config import AppConfig
from .errors import ServiceError

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PreparedAudio:
    """A normalized, temporary WAV and its exact play duration."""

    path: Path
    duration: float
    directory: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


class AudioProcessor:
    """Create 8 kHz mono PCM WAV files without exposing caller input to a shell."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    async def from_message(self, message: str) -> PreparedAudio:
        """Synthesize a message locally with espeak-ng, then normalize it."""
        directory = Path(tempfile.mkdtemp(prefix="talk-alarm-"))
        source = directory / "tts-source.wav"
        output = directory / "alarm.wav"
        try:
            await self._run(
                "espeak-ng",
                "-v",
                self._config.tts_voice,
                "-s",
                str(self._config.tts_speed),
                "-w",
                str(source),
                "--",
                message,
                code="tts_failed",
            )
            self._validate_wav(source, size_limit=self._config.max_audio_bytes)
            await self._normalize(source, output, invalid_status=500)
            return self._prepared(output, directory)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    async def from_url(self, audio_url: str) -> PreparedAudio:
        """Download one allowlisted WAV without redirects and normalize it."""
        parsed = urlsplit(audio_url)
        hostname = (parsed.hostname or "").rstrip(".").lower()
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or hostname not in self._config.allowed_audio_hosts
        ):
            raise ServiceError(
                422,
                "audio_url_not_allowed",
                "The audio URL host is not explicitly allowlisted",
            )

        directory = Path(tempfile.mkdtemp(prefix="talk-alarm-"))
        source = directory / "download.wav"
        output = directory / "alarm.wav"
        timeout = ClientTimeout(total=15, connect=5, sock_read=10)
        connector = TCPConnector(use_dns_cache=False, ttl_dns_cache=0)
        try:
            try:
                async with ClientSession(
                    timeout=timeout, connector=connector, trust_env=False
                ) as session:
                    async with session.get(
                        audio_url,
                        allow_redirects=False,
                        headers={"Accept": "audio/wav, audio/x-wav"},
                    ) as response:
                        if 300 <= response.status < 400:
                            raise ServiceError(
                                422,
                                "audio_redirect_rejected",
                                "Audio URL redirects are not permitted",
                            )
                        if response.status != 200:
                            raise ServiceError(
                                502,
                                "audio_download_failed",
                                "The audio server did not return a WAV file",
                            )
                        length = response.content_length
                        if length is not None and length > self._config.max_audio_bytes:
                            raise ServiceError(
                                422, "audio_too_large", "The WAV exceeds the size limit"
                            )
                        total = 0
                        with source.open("wb") as stream:
                            async for chunk in response.content.iter_chunked(65536):
                                total += len(chunk)
                                if total > self._config.max_audio_bytes:
                                    raise ServiceError(
                                        422,
                                        "audio_too_large",
                                        "The WAV exceeds the size limit",
                                    )
                                stream.write(chunk)
            except ServiceError:
                raise
            except (ClientError, asyncio.TimeoutError, OSError) as err:
                raise ServiceError(
                    502, "audio_download_failed", "Unable to download the WAV"
                ) from err

            self._validate_wav(source, size_limit=self._config.max_audio_bytes)
            await self._normalize(source, output, invalid_status=422)
            return self._prepared(output, directory)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    async def _normalize(
        self, source: Path, output: Path, *, invalid_status: int
    ) -> None:
        try:
            await self._run(
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-map_metadata",
                "-1",
                "-vn",
                "-sn",
                "-dn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "8000",
                "-ac",
                "1",
                str(output),
                code="invalid_audio",
                status=invalid_status,
            )
        except ServiceError:
            raise
        self._validate_wav(output, require_normalized=True)

    def _prepared(self, output: Path, directory: Path) -> PreparedAudio:
        duration = self._validate_wav(output, require_normalized=True)
        return PreparedAudio(path=output, duration=duration, directory=directory)

    def _validate_wav(
        self,
        path: Path,
        *,
        size_limit: int | None = None,
        require_normalized: bool = False,
    ) -> float:
        try:
            if size_limit is not None and path.stat().st_size > size_limit:
                raise ServiceError(422, "audio_too_large", "The WAV exceeds the size limit")
            with wave.open(str(path), "rb") as source:
                if source.getcomptype() != "NONE":
                    raise ServiceError(
                        422, "invalid_audio", "Only uncompressed PCM WAV is supported"
                    )
                rate = source.getframerate()
                frames = source.getnframes()
                if rate <= 0 or frames <= 0:
                    raise ServiceError(422, "invalid_audio", "The WAV contains no audio")
                duration = frames / rate
                if duration > self._config.max_audio_seconds:
                    raise ServiceError(
                        422, "audio_too_long", "The WAV exceeds the duration limit"
                    )
                if require_normalized and (
                    source.getnchannels() != 1
                    or source.getsampwidth() != 2
                    or rate != 8000
                ):
                    raise ServiceError(
                        500, "audio_normalization_failed", "WAV normalization failed"
                    )
                return duration
        except ServiceError:
            raise
        except (OSError, EOFError, wave.Error) as err:
            raise ServiceError(422, "invalid_audio", "The downloaded file is not a valid WAV") from err

    async def _run(
        self,
        executable: str,
        *arguments: str,
        code: str,
        status: int = 500,
    ) -> None:
        """Run a fixed executable with bounded time and no shell expansion."""
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                *arguments,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise ServiceError(status, code, "Audio processing timed out")
        except FileNotFoundError as err:
            raise ServiceError(500, code, "Required audio executable is unavailable") from err
        if process.returncode:
            _LOGGER.warning("Audio helper %s exited with code %s", executable, process.returncode)
            del stderr
            raise ServiceError(status, code, "Audio processing failed")

