"""Load SkillCorner data locally for the CSAS 2027 Data Challenge."""

import os
from contextlib import closing
from getpass import getpass
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx
import pandas as pd


API_URL = "https://skillcorner.com/api/"
EVENT_TABLES = (
    "on_ball_engagements",
    "player_possessions",
    "passing_options",
    "off_ball_runs",
)


def connect_skillcorner(*, use_token=False):
    """Read credentials from the environment or prompt in this kernel."""
    if use_token:
        token = os.getenv("SKILLCORNER_API_TOKEN") or getpass("SkillCorner API token: ")
        if not token.strip():
            raise ValueError("Enter a SkillCorner API token.")
        return httpx.Client(base_url=API_URL, timeout=120, params={"token": token.strip()})

    username = os.getenv("SKILLCORNER_USERNAME") or input("SkillCorner username: ")
    password = os.getenv("SKILLCORNER_PASSWORD") or getpass("SkillCorner password: ")
    if not username.strip() or not password:
        raise ValueError("Enter both a SkillCorner username and password.")
    return httpx.Client(base_url=API_URL, timeout=120, auth=(username.strip(), password))


class SkillCornerHTTPError(RuntimeError):
    def __init__(self, status_code, path):
        self.status_code = status_code
        super().__init__(f"SkillCorner returned HTTP {status_code} for {path}.")


def _send_file_request(client, request, *, stream):
    origin = request.url.netloc
    token = request.url.params.get("token")
    response = client.send(request, stream=stream, follow_redirects=False)
    for _ in range(client.max_redirects):
        redirect = response.next_request
        if redirect is None:
            return response
        response.close()

        target = redirect.url
        if target.scheme != "https" or not target.host or target.username or target.password:
            raise RuntimeError("SkillCorner returned an invalid download redirect.")
        if target.netloc != origin:
            if token and token in target.params.get_list("token"):
                raise RuntimeError("SkillCorner returned a download redirect containing the API token.")
            redirect.headers.pop("Cookie", None)

        # HTTPX strips Authorization on cross-origin redirects. Don't reapply client auth.
        response = client.send(redirect, auth=None, stream=stream, follow_redirects=False)

    if response.next_request is not None:
        response.close()
        raise httpx.TooManyRedirects("SkillCorner exceeded the download redirect limit.")
    return response


def _get(client, endpoint, *, stream=False, file_download=False, **params):
    url = client.base_url.join(endpoint)
    if (
        url.scheme != "https"
        or url.netloc not in {b"skillcorner.com", b"www.skillcorner.com"}
        or not url.path.startswith("/api/")
    ):
        raise ValueError("Expected a SkillCorner HTTPS API URL.")

    # A pagination link can include a token. Use the client's credentials.
    query = url.params.remove("token").merge(params)
    try:
        request = client.build_request("GET", url, params=query)
        if file_download:
            response = _send_file_request(client, request, stream=stream)
        else:
            response = client.send(request, stream=stream, follow_redirects=False)
    except httpx.RequestError as error:
        raise RuntimeError(f"SkillCorner request failed for {url.path} ({type(error).__name__}).") from None
    if not response.is_success:
        response.close()
        raise SkillCornerHTTPError(response.status_code, url.path)
    return response


def _list_records(client, endpoint, **params):
    records = []
    seen = set()
    while endpoint:
        response = _get(client, endpoint, **params)
        page = response.json()
        if isinstance(page, list):
            return records + page

        records.extend(page["results"])
        next_page = page.get("next")
        if next_page:
            next_page = str(response.url.join(next_page))
            if next_page in seen:
                raise RuntimeError("SkillCorner returned a repeated pagination link.")
            seen.add(next_page)
        endpoint, params = next_page, {}
    return records


def get_competition_editions(client):
    """List competition editions available to your account."""
    return pd.json_normalize(_list_records(client, "competition_editions/", user=True))


def get_matches(client, competition_edition_id):
    """List matches in the selected competition edition."""
    return pd.json_normalize(
        _list_records(client, "matches/", competition_edition=int(competition_edition_id))
    )


def get_match(client, match_id):
    """Return match metadata, including players used to interpret tracking IDs."""
    return _get(client, f"match/{int(match_id)}/").json()


def _file_buffer(response):
    payload = response.content
    compression = "gzip" if payload.startswith(b"\x1f\x8b") else None
    return BytesIO(payload), compression


def _tracking_params(data_version):
    params = {"data_version": str(data_version), "file_format": "jsonl"}
    if str(data_version) == "4.0":
        params["frame_rate"] = "10"
    return params


def _event_endpoint(match_id, table):
    if table not in EVENT_TABLES:
        raise ValueError(f"Unknown SkillCorner event table: {table!r}")
    return f"match/{int(match_id)}/dynamic_events/{table}/"


def _get_match_file(client, match_id, dataset, data_version, *, stream=False):
    if dataset == "tracking":
        endpoint = f"match/{int(match_id)}/tracking/"
        params = _tracking_params(data_version)
    else:
        endpoint = _event_endpoint(match_id, dataset)
        params = {"file_format": "csv", "data_version": data_version}

    return _get(client, endpoint, stream=stream, file_download=True, **params)


def match_file_available(client, match_id, dataset, *, data_version=3):
    """Check whether a file is accessible without downloading its body."""
    try:
        with closing(_get_match_file(client, match_id, dataset, data_version, stream=True)) as response:
            return response.status_code != 204
    except SkillCornerHTTPError as error:
        if error.status_code in {403, 404}:
            return False
        raise


def get_tracking(client, match_id, *, data_version=3):
    """Load one match of 10 Hz tracking as one row per frame."""
    response = _get_match_file(client, match_id, "tracking", data_version)
    buffer, compression = _file_buffer(response)
    return pd.read_json(buffer, lines=True, compression=compression, convert_dates=False)


def get_dynamic_event_table(client, match_id, table, *, data_version=3):
    """Load one of the four challenge CSVs."""
    response = _get_match_file(client, match_id, table, data_version)
    buffer, compression = _file_buffer(response)
    try:
        return pd.read_csv(buffer, compression=compression)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def get_dynamic_events(client, match_id, *, data_version=3):
    """Load the four challenge CSVs as a dictionary of DataFrames."""
    return {
        table: get_dynamic_event_table(client, match_id, table, data_version=data_version)
        for table in EVENT_TABLES
    }


def save_match_file(client, match_id, dataset, path, *, data_version=3):
    """Save a match file atomically. Return its path or None if the body is empty."""
    path = Path(path)
    compressed_path = path.with_suffix(path.suffix + ".gz")
    for existing in (path, compressed_path):
        if existing.is_file():
            return existing

    path.parent.mkdir(parents=True, exist_ok=True)
    if dataset == "metadata":
        response = _get(client, f"match/{int(match_id)}/", stream=True)
    else:
        response = _get_match_file(client, match_id, dataset, data_version, stream=True)

    temporary = None
    try:
        with closing(response):
            chunks = response.iter_bytes(chunk_size=1024 * 1024)
            first = next(chunks, b"")
            if not first:
                return None
            if first.startswith(b"\x1f\x8b"):
                path = compressed_path
            with NamedTemporaryFile(dir=path.parent, delete=False) as output:
                temporary = Path(output.name)
                output.write(first)
                for chunk in chunks:
                    output.write(chunk)
            temporary.replace(path)
    except httpx.RequestError as error:
        raise RuntimeError(
            f"SkillCorner download failed for match {match_id} ({type(error).__name__})."
        ) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path
