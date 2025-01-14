from __future__ import annotations

import json
import logging
import os
import shutil
from functools import partial
from pathlib import Path
from subprocess import run  # noqa: S404
from tempfile import mkdtemp
from typing import Any, Dict, List, Optional, Tuple

from janus import Queue
from youtube_dl import YoutubeDL
from youtube_dl.postprocessor.ffmpeg import FFmpegMergerPP, encodeArgument, encodeFilename, prepend_extension
from youtube_dl.utils import sanitize_filename

from .custom_types import DownloadedUpdate, DownloadingUpdate, DownloadResult, UpdateMessage, UpdateStatusCode

logger = logging.getLogger(__name__)
# youtube-dl irritatingly prints log messages directly to stderr/stdout if you don't give it a logger
ytdl_logger = logging.getLogger("ytdl")
ytdl_logger.addHandler(logging.NullHandler())


class AlreadyDownloaded(RuntimeError):
    """Custom exception to indicate that a file/directory already exists, aka it was already downloaded."""

    def __init__(self, msg: str, key: str) -> None:
        """
        Constructor.

        :param msg: Normal exception error message.
        :param key: The pretty name of the download that failed.
        """
        super().__init__(msg)
        self.key = key


def resanitize_string(input_string):
    for ch in [' ','\\','`','*','_','{','}','[',']','(',')','>','#','+','-','.','!','$','"','\'']:
        if ch in input_string:
            input_string = input_string.replace(ch,"")
    return input_string

def process_output_dir(
    download_dir: Path, output_dir: Path, download_video: bool, extract_audio: bool
) -> DownloadResult:
    """
    Parses the output from a youtube-dl run and determines which files are the finalized video and/or audio files.

    Moves these from `download_dir` to the specified destination, effectively deleting intermediate files.

    :param download_dir: Directory that contains all the youtube-dl downloaded files
    :param output_dir: Desired output directory
    :param download_video: Flag indicating whether video is to be retained
    :param extract_audio: Flag indicating whether audio is to be retained
    :return: Tuple containing information and finalized paths for all the files
    """
    # youtube-dl has a really janky API that returns very little information in terms of what was actually downloaded.
    # We therefore have to make some assumptions:
    #  * If video was requested, the preferred video should be a .mkv file
    #  * If audio was requested, it will be a in .mp3
    #  * If video was requested but the source doesn't support separate bestvideo and bestaudio, the video file will be
    #    whatever file was downloaded (using best)
    # We additionally had to tell youtube-dl to not delete intermediate files if we wanted audio so clear those out.
    info_file = list(download_dir.glob("*.json"))[0]
    with info_file.open() as f_in:
        metadata = json.load(f_in)

    pretty_name = metadata["title"]
    sanitized_title = sanitize_filename(pretty_name)
    sanitized_title = resanitize_string(sanitized_title)

    # This was touched during existence checks if subdirectories aren't being made.  If the file exists, it's fine.
    try:
        shutil.move(str(info_file), str(output_dir / f"{sanitized_title}.json"))
        info_file = output_dir / f"{sanitized_title}.json"
    except FileExistsError:
        pass

    audio_file: Optional[Path] = None
    if extract_audio:
        audio_file = list(download_dir.glob("*.mp3"))[0]
        shutil.move(str(audio_file), str(output_dir / f"{sanitized_title}{audio_file.suffix}"))
        audio_file = output_dir / f"{sanitized_title}{audio_file.suffix}"

    video_file: Optional[Path] = None
    # Audio identification performed first otherwise the mp3 would be picked as the fallback option if no mkv present
    if download_video:
        try:
            # Conceivably there are two mkv files, choose the one with the shortest name as youtube-dl includes the
            # format number in the original filename but not the merged output name.
            video_file = sorted(download_dir.glob("*.mkv"), key=lambda x: len(x.name))[0]
        except IndexError:
            # If a merge didn't happen, search for the downloaded streams for one that contains video.  Just assume
            # that only 1 format was downloaded that had video and use it.
            for requested_format in metadata["requested_formats"]:
                if requested_format["vcodec"] != "none":
                    video_file = list(download_dir.glob(f"*.{requested_format['ext']}"))[0]
                    break

        if video_file is not None:
            shutil.move(str(video_file), str(output_dir / f"{sanitized_title}{video_file.suffix}"))
            video_file = output_dir / f"{sanitized_title}{video_file.suffix}"

    return DownloadResult(pretty_name, sanitized_title, info_file, video_file, audio_file)


def process_hook(updates_queue: Queue[UpdateMessage], update: Dict[str, str], req_id: Optional[str] = None) -> None:
    """
    A youtube-dl progress callback hook that puts a slightly reformated update into the `update_queue`.

    :param updates_queue: The queue to put the modified update into
    :param update: The received update from youtube-dl
    :param req_id: Optional request ID that is inserted into the status message as "req_id"
    """
    if update["status"] == "downloading":
        downloading_msg: DownloadingUpdate = {
            "status": UpdateStatusCode.DOWNLOADING,
            "filename": Path(update["filename"]),
            "downloaded_bytes": int(update["downloaded_bytes"]),
            "total_bytes": int(update["total_bytes"]) if update.get("total_bytes") else None,
        }
        if req_id is not None:
            downloading_msg["req_id"] = req_id
        updates_queue.sync_q.put_nowait(downloading_msg)
    elif update["status"] == "finished":
        downloaded_msg: DownloadedUpdate = {"status": UpdateStatusCode.DOWNLOADED, "filename": Path(update["filename"])}
        if req_id is not None:
            downloaded_msg["req_id"] = req_id
        updates_queue.sync_q.put_nowait(downloaded_msg)


def _ffmpeg_monkey_patch(
    self: FFmpegMergerPP, info: Dict[Any, Any], quality: int = 3
) -> Tuple[List[str], Dict[Any, Any]]:
    """
    A rather gross monkey patch to hack in the ability to transcode merged audio to AAC if necessary.

    :param self: This monkey patch is for a class method so this is the normal "self" parameter
    :param info: This is expected by the original method
    :param quality: Extra argument added by the monkey patch, the AAC VBR quality (1-5)
    :return: The expected original method output
    """
    filename = info["filepath"]
    temp_filename = prepend_extension(filename, "temp")

    # Only need to transcode if the source audio isn't already AAC
    if self.get_audio_codec(info["__files_to_merge"][1]) != "aac":
        # Making an assumption that we're using FFmpeg here.  If the Fraunhofer FDK AAC codec is available, prefer it
        encoders_output = run(  # noqa: S603
            [encodeFilename(self.executable), encodeArgument("-encoders")], capture_output=True
        )
        encoder = "libfdk_aac" if encoders_output.stdout.find(b"libfdk_aac") != -1 else "aac"
        args = ["-c", "copy", "-map", "0:v:0", "-map", "1:a:0", "-c:a", encoder, "-q:a", str(quality)]
    else:
        args = ["-c", "copy", "-map", "0:v:0", "-map", "1:a:0"]

    self._downloader.to_screen('[ffmpeg] Merging formats into "%s"' % filename)
    self.run_ffmpeg_multiple_files(info["__files_to_merge"], temp_filename, args)
    os.rename(encodeFilename(temp_filename), encodeFilename(filename))
    return info["__files_to_merge"], info


def download(
    output_dir: Path,
    make_title_subdir: bool,
    url: str,
    download_video: bool,
    extract_audio: bool,
    audio_quality: int = 3,
    updates_queue: Optional[Queue[UpdateMessage]] = None,
    req_id: Optional[str] = None,
    ffmpeg_dir: Optional[Path] = None,
) -> DownloadResult:
    """
    Downloads and transcodes (if necessary) a specified online video or audio clip.

    :param output_dir: Desired output directory
    :param make_title_subdir: Flag indicating whether to create a subdirectory in `output_dir` named after title
    :param url: The URL to attempt to download
    :param download_video: Flag indicating that the video should be downloaded
    :param extract_audio: Flag indicating that a separate audio file (MP3) should be created
    :param audio_quality: The MP3 VBR audio quality (1-5)
    :param updates_queue: A queue to put real-time updates into
    :param req_id: An optional ID to include in all `updates-queue` related updates
    :param ffmpeg_dir: Path to the directory containing FFMPEG binaries
    :return: Tuple containing information and finalized paths for all the files
    """
    if not output_dir.is_dir():
        raise ValueError("output_dir must be a directory")

    postprocessors = [{"key": "FFmpegEmbedSubtitle"}]
    if extract_audio:
        postprocessors.append(
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(audio_quality)}
        )

    progress_hooks = []
    if updates_queue:
        progress_hooks.append(partial(process_hook, updates_queue, req_id=req_id))

    # Can monkey patch to add our transcoding functionality unconditionally as the merger post-processor will only be
    # used if it's necessary.
    FFmpegMergerPP.run = _ffmpeg_monkey_patch

    tmp_out = Path(mkdtemp())
    # Setting both the automatic subs and manual subs is fine, the youtube-dl will prefer manual subs if present
    ytdl_opt = {
        "noplaylist": "true",
        "format": "bestvideo[vcodec^=avc1]+bestaudio/bestvideo+bestaudio/best" if download_video else "bestaudio/best",
        "outtmpl": str(tmp_out) + "/%(title)s.%(ext)s",
        "progress_hooks": progress_hooks,
        "merge_output_format": "mkv",
        "keepvideo": True if download_video else False,
        "postprocessors": postprocessors,
        "logger": ytdl_logger,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en"],
    }

    if ffmpeg_dir:
        ytdl_opt["ffmpeg_location"] = str(ffmpeg_dir)

    try:
        with YoutubeDL(ytdl_opt) as ytdl:
            info = ytdl.extract_info(url, download=False)
            pretty_name = info["title"]
            sanitized_title = sanitize_filename(pretty_name)
            sanitized_title = resanitize_string(sanitized_title)

            try:
                if make_title_subdir:
                    output_dir = output_dir / sanitized_title
                    output_dir.mkdir()
                else:
                    (output_dir / f"{sanitized_title}.json").touch()
            except FileExistsError:
                raise AlreadyDownloaded("File already downloaded", pretty_name)

            with (tmp_out / "info.json").open("w") as f_out:
                json.dump(info, f_out)

            ytdl.download_with_info_file(tmp_out / "info.json")

        download_result = process_output_dir(tmp_out, output_dir, download_video, extract_audio)
    finally:
        shutil.rmtree(tmp_out)

    return download_result
