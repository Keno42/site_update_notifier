import os
import subprocess
import logging

# ロガーの設定
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


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
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=30,
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
        else:
            raise ValueError(
                f"Error retrieving duration for {input_file}:\n{result.stderr}"
            )
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"ffprobe timed out when processing {input_file}")
    except Exception as e:
        raise ValueError(f"Unexpected error processing {input_file}: {str(e)}")


def split_audio_with_overlap(
    input_file: str,
    output_dir: str = "chunks",
    chunk_length_ms: int = 12 * 60_000 + 2_000,  # 12 min + 10 sec
    overlap_ms: int = 2_000,  # 2 sec
) -> list:
    """
    Splits the input audio file into chunks of `chunk_length_ms` duration,
    with each chunk overlapping the last `overlap_ms` of the previous one.
    Utilizes ffmpeg for slicing, storing each chunk in M4A (MP4 container).

    :param input_file: Path to the input .m4a (or any FFmpeg-readable) file.
    :param output_dir: Directory to store the split audio files.
    :param chunk_length_ms: Length of each chunk in milliseconds.
    :param overlap_ms: Overlap duration in milliseconds.
    """

    chunk_paths = []

    # 入力ファイルの存在確認
    if not os.path.exists(input_file):
        raise FileNotFoundError(f"Input file not found: {input_file}")

    # Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)

    try:
        # Calculate total duration
        logger.info(f"Getting duration for {input_file}")
        total_duration_s = get_audio_duration_seconds(input_file)
        if total_duration_s <= 0:
            raise ValueError("Input file has zero or negative duration, cannot split.")

        logger.info(f"Total duration: {total_duration_s:.2f} seconds")

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
            chunk_duration_s = min(chunk_length_s, total_duration_s - start_s)

            if chunk_duration_s <= 0:
                break

            # Create the output path
            chunk_name = f"chunk_{chunk_index:03d}.m4a"
            output_path = os.path.join(output_dir, chunk_name)

            logger.info(
                f"Splitting chunk {chunk_index} - Start: {start_s:.2f}s, Duration: {chunk_duration_s:.2f}s"
            )

            try:
                # 大きなファイルの効率的な処理のためのffmpegコマンド最適化
                command = [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",  # エラーのみをログに出力
                    "-ss",
                    str(start_s),  # 開始時間
                    "-i",
                    input_file,  # 入力ファイル
                    "-t",
                    str(chunk_duration_s),  # 長さ
                    "-c:a",
                    "aac",  # AACエンコーダー
                    "-b:a",
                    "128k",  # オーディオビットレート
                    "-threads",
                    "4",  # 使用するスレッド数を制限
                    "-max_muxing_queue_size",
                    "1024",  # キューサイズを増やす
                    "-f",
                    "mp4",  # MP4コンテナを強制
                    output_path,
                ]

                # サブプロセスの実行とタイムアウト設定
                subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    timeout=600,  # より長いタイムアウト (10分)
                )

                logger.info(
                    f"Exported {output_path} (start={start_s:.2f}s "
                    f"duration={chunk_duration_s:.2f}s)"
                )

                chunk_paths.append(output_path)

                # Move to the next chunk start
                start_s += step_s
                chunk_index += 1

            except subprocess.TimeoutExpired:
                logger.error(f"FFmpeg timed out processing chunk {chunk_index}")
                error_msg = f"FFmpeg timed out when processing chunk {chunk_index}"
                raise TimeoutError(error_msg)
            except subprocess.CalledProcessError as e:
                error_msg = e.stderr.decode() if e.stderr else "Unknown error"
                logger.error(f"FFmpeg error for chunk {chunk_index}: {error_msg}")
                raise RuntimeError(f"FFmpeg error for chunk {chunk_index}: {error_msg}")

    except Exception as e:
        logger.error(f"Error during audio splitting: {str(e)}")
        raise

    logger.info(f"Successfully split {input_file} into {len(chunk_paths)} chunks")
    return chunk_paths
