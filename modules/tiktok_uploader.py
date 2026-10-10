"""
TikTok uploader module.

Sandbox mode mein videos DRAFT ke roop mein jayengi.
User ko TikTok app kholke manually "Post" karna padega.
"""

import os
import time
import requests

TIKTOK_API_BASE = "https://open.tiktokapis.com/v2"


# ---------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------
def refresh_access_token(client_key, client_secret, refresh_token):
    """
    Refresh token se naya access token lo.
    Access token 24 ghante chalta hai, refresh token 365 din.
    """
    url = f"{TIKTOK_API_BASE}/oauth/token/"
    resp = requests.post(
        url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    data = resp.json()

    if "access_token" not in data:
        raise RuntimeError(f"TikTok token refresh failed: {data}")

    print(f"✓ TikTok access token refreshed (expires in {data.get('expires_in')}s)")
    return data["access_token"], data.get("refresh_token", refresh_token)


# ---------------------------------------------------------------
# Upload video (FILE_UPLOAD method)
# ---------------------------------------------------------------
def upload_to_tiktok(
    video_path,
    title,
    client_key=None,
    client_secret=None,
    refresh_token=None,
):
    """
    Video ko TikTok par upload karta hai (draft mode).

    Args:
        video_path: local video file ka path
        title: video ka caption/title
        client_key, client_secret, refresh_token: TikTok credentials

    Returns:
        publish_id (str) — TikTok ka publish ID
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video file not found: {video_path}")

    client_key = client_key or os.getenv("TIKTOK_CLIENT_KEY")
    client_secret = client_secret or os.getenv("TIKTOK_CLIENT_SECRET")
    refresh_token = refresh_token or os.getenv("TIKTOK_REFRESH_TOKEN")

    print(f"✓ TIKTOK_CLIENT_KEY exists: {bool(client_key)}")
    print(f"✓ TIKTOK_CLIENT_SECRET exists: {bool(client_secret)}")
    print(f"✓ TIKTOK_REFRESH_TOKEN exists: {bool(refresh_token)}")

    if not (client_key and client_secret and refresh_token):
        raise RuntimeError(
            "TikTok credentials missing. Check GitHub Secrets: "
            "TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET, TIKTOK_REFRESH_TOKEN"
        )

    # Step 1: Access token refresh karo
    access_token, _ = refresh_access_token(
        client_key, client_secret, refresh_token
    )

    file_size = os.path.getsize(video_path)

    # TikTok limit: max 4GB
    if file_size > 4 * 1024 * 1024 * 1024:
        raise RuntimeError(f"Video too large for TikTok: {file_size} bytes")

    # Step 2: Upload init
    print("📤 Initializing TikTok upload...")
    init_url = f"{TIKTOK_API_BASE}/post/publish/video/init/"
    init_resp = requests.post(
        init_url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json={
            "post_info": {
                "title": title[:2200],  # TikTok caption limit
                "privacy_level": "SELF_ONLY",  # Sandbox: only me
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
                "video_cover_timestamp_ms": 1000,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": file_size,
                "chunk_size": file_size,
                "total_chunk_count": 1,
            },
        },
        timeout=60,
    )
    init_data = init_resp.json()

    if "data" not in init_data or "upload_url" not in init_data.get("data", {}):
        raise RuntimeError(f"TikTok init failed: {init_data}")

    upload_url = init_data["data"]["upload_url"]
    publish_id = init_data["data"]["publish_id"]
    print(f"✓ Upload URL received. Publish ID: {publish_id}")

    # Step 3: Video binary upload
    print(f"📤 Uploading {file_size} bytes to TikTok...")
    with open(video_path, "rb") as f:
        video_data = f.read()

    upload_resp = requests.put(
        upload_url,
        headers={
            "Content-Type": "video/mp4",
            "Content-Length": str(file_size),
            "Content-Range": f"bytes 0-{file_size - 1}/{file_size}",
        },
        data=video_data,
        timeout=300,
    )

    if upload_resp.status_code not in (200, 201, 206):
        raise RuntimeError(
            f"TikTok upload failed: {upload_resp.status_code} - "
            f"{upload_resp.text[:500]}"
        )

    print("✓ Video bytes uploaded")

    # Step 4: Status check (optional)
    time.sleep(3)
    status_url = f"{TIKTOK_API_BASE}/post/publish/status/fetch/"
    status_resp = requests.post(
        status_url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json={"publish_id": publish_id},
        timeout=30,
    )
    status_data = status_resp.json()
    print(f"✓ TikTok status: {status_data.get('data', {}).get('status', 'unknown')}")

    print(f"🎉 TikTok upload complete! Publish ID: {publish_id}")
    print(f"📱 Open TikTok app to manually post the draft")

    return publish_id
