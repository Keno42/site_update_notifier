import os
import subprocess


def get_audio_duration_seconds(input_file: str) -> float:
    """
    Returns the duration (in seconds) of the given audio file by calling ffprobe.
    Raises ValueError if ffprobe fails.
    """
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        input_file,
    ]
    result = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True
    )
    if result.returncode == 0:
        return float(result.stdout.strip())
    else:
        raise ValueError(
            f"Error retrieving duration for {input_file}:\n{result.stderr}"
        )


def split_audio_with_overlap(
    input_file: str,
    output_dir: str = "chunks",
    chunk_length_ms: int = 20 * 60_000 + 10_000,  # 20 min + 10 sec
    overlap_ms: int = 10_000,  # 10 sec
) -> None:
    """
    Splits the input audio file into chunks of `chunk_length_ms` duration,
    with each chunk overlapping the last `overlap_ms` of the previous one.
    Utilizes ffmpeg for slicing, storing each chunk in M4A (MP4 container).

    :param input_file: Path to the input .m4a (or any FFmpeg-readable) file.
    :param output_dir: Directory to store the split audio files.
    :param chunk_length_ms: Length of each chunk in milliseconds.
    :param overlap_ms: Overlap duration in milliseconds.
    """

    # Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)

    # Calculate total duration
    total_duration_s = get_audio_duration_seconds(input_file)
    if total_duration_s <= 0:
        raise ValueError("Input file has zero or negative duration, cannot split.")

    # Convert ms to seconds
    chunk_length_s = chunk_length_ms / 1000.0
    overlap_s = overlap_ms / 1000.0
    step_s = chunk_length_s - overlap_s

    # Start splitting
    start_s = 0.0
    chunk_index = 1

    while start_s < total_duration_s:
        # Calculate this chunk's actual duration in seconds
        # (if near the end of the file, we may have a shorter final chunk)
        chunk_duration_s = chunk_length_s
        if (start_s + chunk_duration_s) > total_duration_s:
            chunk_duration_s = total_duration_s - start_s

        if chunk_duration_s <= 0:
            break

        # Create the output path
        chunk_name = f"chunk_{chunk_index:03d}.m4a"
        output_path = os.path.join(output_dir, chunk_name)

        # Build and run ffmpeg command
        # -ss: start time
        # -t: duration
        # -c:a aac: encode with AAC
        # -b:a 128k: set audio bitrate
        # -f mp4: force MP4 container, but the output file uses .m4a extension
        command = [
            "ffmpeg",
            "-y",
            "-ss",
            str(start_s),
            "-t",
            str(chunk_duration_s),
            "-i",
            input_file,
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-f",
            "mp4",
            output_path,
        ]

        subprocess.run(command, check=True)
        print(
            f"Exported {output_path} (start={start_s:.2f}s "
            f"duration={chunk_duration_s:.2f}s)"
        )

        # Move to the next chunk start
        start_s += step_s
        chunk_index += 1
