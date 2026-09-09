from google.cloud import storage
import subprocess
import tempfile
from pathlib import Path

BUCKET_NAME = "falco-video-output-549259282828"

CLIPS = [
    "falco/675ad1f2-b7d3-4e5a-8ecb-d3521f22d043.mp4",
    "falco/e31bd3de-81bf-4551-aa08-e3d62e372071.mp4",
    "falco/54d2832f-76cb-436d-a1b4-b29ca02e30df.mp4",
]

client = storage.Client()
bucket = client.bucket(BUCKET_NAME)

with tempfile.TemporaryDirectory() as tmpdir:

    for i, filename in enumerate(CLIPS, start=1):

        local_path = Path(tmpdir) / f"clip_{i}.mp4"

        bucket.blob(filename).download_to_filename(
            str(local_path)
        )

        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(local_path)
            ],
            capture_output=True,
            text=True,
            check=True
        )

        duration = float(result.stdout.strip())

        print(
            f"PLAN {i} | "
            f"start=0 | "
            f"duration={duration:.3f} | "
            f"{filename}"
        )