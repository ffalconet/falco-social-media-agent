from google.cloud import storage
from datetime import timezone

BUCKET_NAME = "falco-video-output-549259282828"

client = storage.Client()
bucket = client.bucket(BUCKET_NAME)

blobs = bucket.list_blobs(prefix="falco/")

videos = []

for blob in blobs:
    if blob.name.endswith(".mp4"):
        videos.append({
            "name": blob.name,
            "size_mb": round(blob.size / 1024 / 1024, 2) if blob.size else None,
            "updated": blob.updated.astimezone(timezone.utc).isoformat()
            if blob.updated else None
        })

videos.sort(
    key=lambda x: x["updated"] or "",
    reverse=True
)

for video in videos[:30]:
    print(
        video["updated"],
        video["size_mb"],
        "MB",
        video["name"]
    )