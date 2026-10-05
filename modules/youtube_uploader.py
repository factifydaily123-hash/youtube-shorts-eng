import os
import pickle
import logging
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# SCOPES — Smart handling
# ------------------------------------------------------------------
# Problem: Agar refresh token sirf "youtube.upload" scope ke saath
# generate hua hai, aur hum "youtube" (full) scope maangein, to
# Google "invalid_scope" error deta hai.
#
# Solution: Primary scope = youtube.upload (jo har refresh token ke
# paas hota hai). Full scope sirf tab use karo jab explicitly chahiye
# ho (thumbnail/playlist) aur wo fail ho to gracefully skip karo.
#
# NOTE: Thumbnail set karne ke liye "youtube" scope chahiye hota hai,
# lekin agar tumhara token sirf upload scope ka hai, to thumbnail
# skip ho jayega — upload phir bhi kaam karega.
# ------------------------------------------------------------------

UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
FULL_SCOPE = "https://www.googleapis.com/auth/youtube"

# Yeh scopes refresh token maangte waqt use hote hain (OAuth flow).
# Sirf UPLOAD_SCOPE use kar rahe hain taake tumhara existing token
# bhi kaam kare. Agar naya token full scope ke saath banaoge to
# thumbnail/playlist bhi chalega.
SCOPES = [UPLOAD_SCOPE]


# ------------------------------------------------------------------
# Service builder
# ------------------------------------------------------------------
def get_youtube_service(client_id=None, client_secret=None, refresh_token=None,
                        require_full_scope=False):
    """
    YouTube API service build karta hai.

    Args:
        client_id, client_secret, refresh_token: credentials
        require_full_scope: agar True, to FULL_SCOPE try karega
                            (thumbnail/playlist ke liye).

    Returns:
        youtube service object
    """
    creds = None

    c_id = client_id or os.getenv("YOUTUBE_CLIENT_ID")
    c_secret = client_secret or os.getenv("YOUTUBE_CLIENT_SECRET")
    r_token = refresh_token or os.getenv("YOUTUBE_REFRESH_TOKEN")

    print(f"✓ YOUTUBE_CLIENT_ID exists: {bool(c_id)}")
    print(f"✓ YOUTUBE_CLIENT_SECRET exists: {bool(c_secret)}")
    print(f"✓ YOUTUBE_REFRESH_TOKEN exists: {bool(r_token)}")

    # Scopes: agar full scope chahiye to dono try karo, warna sirf upload
    scopes_to_try = [UPLOAD_SCOPE, FULL_SCOPE] if require_full_scope else [UPLOAD_SCOPE]

    if c_id and c_secret and r_token:
        last_error = None
        for scope in scopes_to_try:
            try:
                test_creds = Credentials(
                    token=None,
                    refresh_token=r_token,
                    client_id=c_id,
                    client_secret=c_secret,
                    token_uri="https://oauth2.googleapis.com/token",
                    scopes=[scope],
                )
                test_creds.refresh(Request())
                creds = test_creds
                print(f"✓ Token refreshed successfully with scope: {scope}")
                break
            except Exception as e:
                last_error = e
                err_str = str(e)
                if "invalid_scope" in err_str:
                    print(f"⚠️ Scope '{scope}' refresh token ke saath compatible nahi hai.")
                else:
                    print(f"⚠️ Token refresh warning ({scope}): {e}")

        if creds is None:
            print(f"❌ Sabhi scopes fail ho gaye. Last error: {last_error}")
    else:
        # Fallback: token.pickle se load karo (local testing ke liye)
        if os.path.exists("token.pickle"):
            with open("token.pickle", "rb") as token:
                creds = pickle.load(token)
            print("✓ Loaded credentials from token.pickle")

    # Agar creds valid nahi hain, to OAuth flow chalao (local only)
    if not creds or not creds.valid:
        if creds and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                print(f"Refresh failed: {e}")

        if not creds or not creds.valid:
            if os.path.exists("client_secret.json"):
                flow = InstalledAppFlow.from_client_secrets_file(
                    "client_secret.json", SCOPES
                )
                creds = flow.run_local_server(port=0)
                with open("token.pickle", "wb") as token:
                    pickle.dump(creds, token)
            else:
                raise Exception(
                    "❌ YouTube Credentials ghayab hain!\n"
                    "GitHub Secrets check karein: YOUTUBE_CLIENT_ID, "
                    "YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN"
                )

    return build("youtube", "v3", credentials=creds)


# ------------------------------------------------------------------
# Upload video
# ------------------------------------------------------------------
def upload_video(video_path, title, description, tags=None, category_id="22",
                 privacy_status="public", client_id=None, client_secret=None,
                 refresh_token=None):
    """
    Video ko YouTube par upload karta hai.

    Returns:
        video_id (str) — uploaded video ka ID
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video file not found: {video_path}")

    if tags is None:
        tags = ["shorts", "youtubeshorts"]

    youtube = get_youtube_service(client_id, client_secret, refresh_token)

    # YouTube title limit: 100 chars, tags: 500 chars total
    title = title[:100]
    tags = tags[:15] if len(tags) > 15 else tags

    body = {
        "snippet": {
            "title": title,
            "description": description[:5000],
            "tags": tags,
            "categoryId": category_id,
            "defaultLanguage": "en",
            "defaultAudioLanguage": "en",
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
            "madeForKids": False,
        },
    }

    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)

    print(f"📤 Uploading '{title}'...")
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    last_progress = -1
    retry_count = 0
    max_retries = 5

    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                progress = int(status.progress() * 100)
                if progress != last_progress:
                    print(f"   Uploaded {progress}%")
                    last_progress = progress
            retry_count = 0  # reset on success
        except HttpError as e:
            if e.resp.status in [500, 502, 503, 504]:
                retry_count += 1
                if retry_count > max_retries:
                    raise
                import time as _time
                wait = 2 ** retry_count
                print(f"⚠️ Server error {e.resp.status}, retrying in {wait}s...")
                _time.sleep(wait)
                # Resume upload
                status, response = request.next_chunk()
            else:
                raise
        except Exception as e:
            retry_count += 1
            if retry_count > max_retries:
                raise
            import time as _time
            wait = 2 ** retry_count
            print(f"⚠️ Upload error: {e}. Retrying in {wait}s...")
            _time.sleep(wait)

    video_id = response.get("id")
    print(f"✅ Video uploaded! ID: {video_id}")
    print(f"🔗 https://youtube.com/shorts/{video_id}")
    return video_id


# ------------------------------------------------------------------
# Thumbnail (optional — needs full youtube scope)
# ------------------------------------------------------------------
def set_thumbnail(video_id, thumbnail_path, client_id=None, client_secret=None,
                  refresh_token=None):
    """
    Custom thumbnail set karta hai.

    NOTE: Iske liye refresh token mein FULL_SCOPE hona chahiye.
    Agar nahi hai to yeh function silently skip ho jayega (crash nahi karega).
    """
    if not os.path.exists(thumbnail_path):
        print(f"⚠️ Thumbnail file nahi mili: {thumbnail_path}")
        return False

    # Thumbnail size limit: 2MB
    if os.path.getsize(thumbnail_path) > 2 * 1024 * 1024:
        print(f"⚠️ Thumbnail 2MB se bada hai, skip kar rahe hain.")
        return False

    try:
        youtube = get_youtube_service(
            client_id, client_secret, refresh_token,
            require_full_scope=True,
        )
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(thumbnail_path),
        ).execute()
        print(f"✅ Thumbnail set for video {video_id}")
        return True
    except HttpError as e:
        # Agar scope issue hai ya channel verified nahi hai, to skip
        if "insufficient" in str(e).lower() or "forbidden" in str(e).lower():
            print(f"⚠️ Thumbnail set nahi ho paya (channel verified nahi hai ya scope issue).")
        else:
            print(f"⚠️ Thumbnail set failed: {e}")
        return False
    except Exception as e:
        print(f"⚠️ Thumbnail set failed: {e}")
        return False


# ------------------------------------------------------------------
# Playlist (optional — needs full youtube scope)
# ------------------------------------------------------------------
def add_to_playlist(video_id, playlist_id, client_id=None, client_secret=None,
                    refresh_token=None):
    """
    Video ko playlist mein add karta hai.

    NOTE: Iske liye refresh token mein FULL_SCOPE hona chahiye.
    """
    if not playlist_id:
        print("⚠️ Playlist ID nahi di gayi, skip.")
        return False

    try:
        youtube = get_youtube_service(
            client_id, client_secret, refresh_token,
            require_full_scope=True,
        )
        youtube.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {
                        "kind": "youtube#video",
                        "videoId": video_id,
                    },
                }
            },
        ).execute()
        print(f"✅ Video added to playlist {playlist_id}")
        return True
    except HttpError as e:
        if "insufficient" in str(e).lower() or "forbidden" in str(e).lower():
            print(f"⚠️ Playlist add nahi ho paya (scope issue).")
        else:
            print(f"⚠️ Playlist add failed: {e}")
        return False
    except Exception as e:
        print(f"⚠️ Playlist add failed: {e}")
        return False


# ------------------------------------------------------------------
# Utility: check what scopes current token has
# ------------------------------------------------------------------
def check_token_scopes(client_id=None, client_secret=None, refresh_token=None):
    """
    Debug helper: batata hai ki current refresh token ke paas
    kaunse scopes hain.
    """
    import requests

    c_id = client_id or os.getenv("YOUTUBE_CLIENT_ID")
    c_secret = client_secret or os.getenv("YOUTUBE_CLIENT_SECRET")
    r_token = refresh_token or os.getenv("YOUTUBE_REFRESH_TOKEN")

    if not (c_id and c_secret and r_token):
        print("❌ Credentials missing")
        return None

    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": c_id,
            "client_secret": c_secret,
            "refresh_token": r_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    data = resp.json()

    if "access_token" not in data:
        print(f"❌ Token refresh failed: {data}")
        return None

    # Token info endpoint se scopes check karo
    info = requests.get(
        "https://oauth2.googleapis.com/tokeninfo",
        params={"access_token": data["access_token"]},
        timeout=30,
    ).json()

    scopes = info.get("scope", "").split()
    print(f"✓ Current refresh token scopes:")
    for s in scopes:
        print(f"   - {s}")

    has_upload = UPLOAD_SCOPE in scopes
    has_full = FULL_SCOPE in scopes
    print(f"\n✓ Can upload: {has_upload}")
    print(f"✓ Can set thumbnail / playlist: {has_full}")

    if not has_full:
        print("\n💡 Tip: Naya refresh token generate karo FULL_SCOPE ke saath")
        print("   taake thumbnail aur playlist features bhi kaam karein.")

    return scopes


if __name__ == "__main__":
    # Debug mode: python -m modules.youtube_uploader
    print("🔍 Checking YouTube token scopes...\n")
    check_token_scopes()
