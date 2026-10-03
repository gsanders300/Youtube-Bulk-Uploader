"""OAuth 2.0 authentication for YouTube API."""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import cast

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build

from .utils import get_config_dir, get_project_root

logger = logging.getLogger("youtube_uploader")

# YouTube upload scope
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# Legacy source-checkout paths retained for backward compatibility.
DEFAULT_CREDENTIALS_FILE = "config/client_secrets.json"
DEFAULT_TOKEN_FILE = "config/token.json"


def get_default_credentials_path() -> Path:
    """Get the credentials path, retaining compatibility with source checkouts."""
    legacy_path = get_project_root() / DEFAULT_CREDENTIALS_FILE
    if legacy_path.exists():
        return legacy_path
    return get_config_dir() / "client_secrets.json"


def get_default_token_path() -> Path:
    """Get the token path, retaining compatibility with source checkouts."""
    legacy_path = get_project_root() / DEFAULT_TOKEN_FILE
    if legacy_path.exists():
        return legacy_path
    return get_config_dir() / "token.json"


def validate_credentials_file(credentials_file: Path) -> bool:
    """
    Check if credentials file exists and has required fields.

    Args:
        credentials_file: Path to client_secrets.json

    Returns:
        True if valid, False otherwise
    """
    credentials_file = Path(credentials_file)

    if not credentials_file.exists():
        logger.error(f"Credentials file not found: {credentials_file}")
        return False

    try:
        with credentials_file.open(encoding="utf-8") as f:
            data = json.load(f)

        # Check for required OAuth fields
        if "installed" in data:
            required = ["client_id", "client_secret"]
            client_data = data["installed"]
        elif "web" in data:
            required = ["client_id", "client_secret"]
            client_data = data["web"]
        else:
            logger.error("Invalid credentials format: missing 'installed' or 'web' key")
            return False

        for field in required:
            if field not in client_data:
                logger.error(f"Missing required field: {field}")
                return False

        return True

    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in credentials file: {e}")
        return False
    except Exception as e:
        logger.error(f"Error reading credentials file: {e}")
        return False


def load_token(token_file: Path) -> Credentials | None:
    """
    Load existing token from file.

    Args:
        token_file: Path to token.json

    Returns:
        Credentials if valid, None otherwise
    """
    token_file = Path(token_file)

    if not token_file.exists():
        logger.debug("No existing token file found")
        return None

    try:
        with token_file.open(encoding="utf-8") as f:
            token_data = json.load(f)

        creds = Credentials.from_authorized_user_info(token_data, SCOPES)
        logger.debug("Loaded existing credentials from token file")
        return cast(Credentials, creds)

    except Exception as e:
        logger.warning(f"Could not load token file: {e}")
        return None


def save_token(creds: Credentials, token_file: Path) -> None:
    """
    Save credentials to token file.

    Args:
        creds: Credentials to save
        token_file: Path to save to
    """
    token_file = Path(token_file)

    # Ensure directory exists
    token_file.parent.mkdir(parents=True, exist_ok=True)

    # Write beside the destination and replace atomically so interruption cannot
    # leave a truncated credentials file.  chmod is best-effort on Windows.
    fd, temporary_name = tempfile.mkstemp(
        dir=token_file.parent, prefix=f".{token_file.name}.", text=True
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
            f.flush()
            os.fsync(f.fileno())
        try:
            temporary_path.chmod(0o600)
        except OSError:
            logger.debug("Could not restrict token permissions on this platform")
        os.replace(temporary_path, token_file)
    finally:
        temporary_path.unlink(missing_ok=True)

    logger.info(f"Saved credentials to {token_file}")


def refresh_credentials(creds: Credentials) -> Credentials | None:
    """
    Attempt to refresh expired credentials.

    Args:
        creds: Expired credentials with refresh token

    Returns:
        Refreshed credentials or None if refresh failed
    """
    try:
        creds.refresh(Request())
        logger.info("Successfully refreshed credentials")
        return creds
    except Exception as e:
        logger.warning(f"Could not refresh credentials: {e}")
        return None


def run_oauth_flow(credentials_file: Path) -> Credentials:
    """
    Run OAuth 2.0 flow with local server callback.

    Opens browser for user authorization.

    Args:
        credentials_file: Path to client_secrets.json

    Returns:
        New credentials
    """
    logger.info("Starting OAuth flow - a browser window will open")

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)

    # port=0 lets the OS assign any free port, avoiding failures when 8080 is
    # already in use by another process. prompt="consent" ensures a refresh
    # token is always returned, even if the user previously granted access.
    creds = flow.run_local_server(
        port=0,
        prompt="consent",
        success_message="Authentication successful! You can close this window.",
    )

    logger.info("OAuth flow completed successfully")
    return cast(Credentials, creds)


def get_authenticated_service(
    credentials_file: str | None = None, token_file: str | None = None
) -> Resource:
    """
    Get authenticated YouTube API service.

    Flow:
    1. Check for existing token.json
    2. If valid, use it
    3. If expired, attempt refresh
    4. If no token or refresh fails, run OAuth flow
    5. Save new token for future use
    6. Return YouTube service object

    Args:
        credentials_file: Path to client_secrets.json (optional)
        token_file: Path to token.json (optional)

    Returns:
        Authenticated YouTube API service

    Raises:
        FileNotFoundError: If credentials file not found
        ValueError: If credentials file is invalid
    """
    # Use default paths if not specified
    creds_path = (
        Path(credentials_file) if credentials_file else get_default_credentials_path()
    )
    token_path = Path(token_file) if token_file else get_default_token_path()

    # Try to load existing token
    creds = load_token(token_path)

    # Check if credentials need refresh or new auth
    if creds and creds.valid:
        logger.info("Using existing valid credentials")
    elif creds and creds.expired and creds.refresh_token:
        creds = refresh_credentials(creds)
        if creds:
            save_token(creds, token_path)
        else:
            creds = None

    # Run OAuth flow if needed
    if not creds:
        if not validate_credentials_file(creds_path):
            raise FileNotFoundError(
                f"Valid credentials file not found at {creds_path}. "
                "Please download OAuth credentials from Google Cloud Console."
            )
        creds = run_oauth_flow(creds_path)
        save_token(creds, token_path)

    # Build and return YouTube service.
    # cache_discovery=False avoids a DiscoveryCache warning in environments
    # where the default file cache is unavailable (most desktop installs).
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    logger.info("YouTube API service initialized")

    return service
