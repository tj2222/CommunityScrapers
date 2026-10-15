import json
import re
import sys
from datetime import datetime

import py_common.log as log
from py_common import graphql
from py_common.config import get_config
from py_common.proxy import StashRequests
from py_common.types import ScrapedPerformer, ScrapedScene, ScrapedTag
from py_common.util import scraper_args

# CONFIG_NOTES:
#
# COOKIES:
# Cookies should be set as a semicolon-delimited string of name=value pair(s) without any newlines.
# You may copy and paste Chrome network tools 'Request' tab "Cookie:" value.
# Cookie `session_id` is required. Others are ignored.
#
# CREATE_MARKERS:
# Whether to automatically create scene markers from video tags (default: false).
#
# CREATE_MISSING_TAGS:
# Whether to create tags in Stash for new markers if a matching tag does not exist (default: false).

config = get_config(
    default="""
    # See CONFIG_NOTES in NVGNetwork.py for documentation
    COOKIES =
    CREATE_MARKERS = false
    CREATE_MISSING_TAGS = false
"""
)


requests = StashRequests()


def get_cookies_dict() -> dict[str, str]:
    required_cookies = [
        "session_id",
    ]
    config_val = config.config_dict.get("COOKIES") or ""
    cookies: dict[str, str] = {}
    for part in str(config_val).split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            if k in required_cookies:
                cookies[k] = v.strip()

    missing_cookies = [c for c in required_cookies if not cookies.get(c)]
    if missing_cookies:
        log.error(f"Missing necessary cookies: {', '.join(missing_cookies)}")
        return {}
    return cookies


def extract_scene_id(url: str) -> str | None:
    match = re.search(r"vip\.netvideogirls\.com/(?:members/)?video/(\d+)", url)
    if match:
        return match.group(1)
    return None


def fetch_scene_json(scene_id: str) -> dict | None:
    cookies = get_cookies_dict()
    if not cookies:
        log.error("Exiting due to missing cookies")
        return None

    api_url = f"https://vip.netvideogirls.com/api/video/{scene_id}"
    log.debug(f"Fetching scene API: {api_url}")
    try:
        response = requests.get(api_url, cookies=cookies, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        log.error(f"Failed to fetch scene {scene_id} from API: {e}")
        return None



def parse_release_date(raw_date: str | None) -> str | None:
    if not raw_date:
        return None
    try:
        dt = datetime.strptime(raw_date.strip(), "%b %d, %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        log.warning(f"Could not parse release date: {raw_date}")
        return None


MALE_PERFORMER_TAG_TO_NAME_MAP = {
    # As identified on IAFD and StashDB.
    "apollo": "Apollo Banks",
    "chris": "Tee Reel",
    "d": "Lil D.",
    "dan": "Rion King",
    "derek": "Derek Savage",
    "donnie": "Donnie Rock",
    "ezekiel": "Ezekiel",
    "grant": "Grant",
    "jason": "Lucas Frost",
    "javon": "Jovan Jordan",
    "jay": "Jay Hefner",
    "jesse": "Jesse",
    "john": "John",
    "jonte": "Jax Slayher", # A couple of times, "jonte" has been Isiah Maxwell, but it is almost always Jax.
    "josh": "Conor Coxxx",
    "kaby": "Kaby Snow",
    "kai": "Kai Jaxon",
    "ken": "Ken Feels",
    "lawson": "Lawson Jones",
    "leroy": "Leroy",
    "lionel": "Lionel Piper",
    "mark": "Seth Gamble",
    "matt": "Matt Sloan",
    "mazee": "Mazee the Goat",
    "mike": "Mike",
    "nade": "Nade Nasty",
    "oliver": "Oliver Flynn",
    "preston": "Preston Parker",
    "rico": "Rico Hernandez",
    "rob": "Rob",
    "robby": "Robby", # May be Robby Apples and/or may be same person as Ryan.
    "ryan": "Ryan",
    "sloan": "Sloane Stone",
    "tony": "Tony Profane",
    "troy": "Troy Francisco",
    "tyler": "Tyler D.",
    "zack": "Donnie Cabo",
}

def get_performers(scene_data: dict) -> list[ScrapedPerformer]:
    # Find female performers in "models" list
    performers: list[ScrapedPerformer] = []
    models = scene_data.get("models") or []
    for model in models:
        model_name = model.get("model_name")
        if not model_name:
            continue
        performer: ScrapedPerformer = {
            "name": model_name,
            "gender": "FEMALE",
        }
        performers.append(performer)

    # Find male performers in "general_tags" list
    for tag in scene_data.get("general_tags") or []:
        tag_obj = tag.get("tag") or {}
        tag_name = tag_obj.get("tag_name")
        if tag_name:
            tag_name = tag_name.strip()
            if tag_name.lower() in MALE_PERFORMER_TAG_TO_NAME_MAP:
                performer: ScrapedPerformer = {
                    "name": MALE_PERFORMER_TAG_TO_NAME_MAP[tag_name.lower()],
                    "gender": "MALE",
                }
                performers.append(performer)

    return performers


def get_tags(scene_data: dict) -> list[ScrapedTag]:
    tags_seen: set[str] = set()
    tag_list: list[ScrapedTag] = []
    all_tags = (scene_data.get("general_tags") or []) + (scene_data.get("video_tags") or [])
    for tag in all_tags:
        tag_obj = tag.get("tag") or {}
        tag_name = tag_obj.get("tag_name")
        if tag_name:
            normalized = tag_name.strip()
            if normalized and normalized.lower() not in tags_seen:
                tags_seen.add(normalized.lower())
                tag_list.append({"name": normalized})

    # All NVG metadata is paywalled
    tag_list.append({"name": "Members Only"})
    # NVG always releases scenes with a temporary title of "omg the latest[...]", then sets a title a week or so later.
    # Consider the temporary titles to be stand-ins for an "unknown" title.
    if scene_data.get("title", "").lower().startswith("omg the latest"):
        tag_list.append({"name": "Unknown Title"})

    return tag_list


def get_cover_image(scene_data: dict) -> str | None:
    featured = scene_data.get("featured_image")
    if featured and featured.get("thumb_name"):
        thumb_name = featured["thumb_name"]
        poster_name = thumb_name.replace("med", "poster")
        return f"https://static.netvideogirls.com/{poster_name}"
    scene_id = scene_data.get("id")
    if scene_id:
        return f"https://static.netvideogirls.com/{scene_id}-1-poster.jpg"
    return None


def create_markers(stash_scene_id: str, scene_data: dict) -> None:
    video_tags = scene_data.get("video_tags") or []
    if not video_tags:
        log.debug("No video_tags found for markers")
        return

    create_missing_tags = bool(config.config_dict.get("CREATE_MISSING_TAGS"))

    log.debug("Querying all tags from Stash")
    raw_tags = graphql.callGraphQL("query allTags { allTags { name id aliases } }")
    if not raw_tags or "allTags" not in raw_tags:
        log.error("Failed to query tags from Stash via GraphQL")
        return

    tags_map: dict[str, str] = {
        t["name"].lower(): t["id"] for t in raw_tags["allTags"]
    }
    for t in raw_tags["allTags"]:
        for alias in t.get("aliases") or []:
            tags_map[alias.lower()] = t["id"]

    log.debug(f"Querying existing markers for Stash scene {stash_scene_id}")
    find_scene_query = """
    query FindScene($id: ID!) {
        findScene(id: $id) {
            scene_markers {
                title
                seconds
            }
        }
    }
    """
    scene_res = graphql.callGraphQL(find_scene_query, {"id": stash_scene_id})
    if not scene_res or not scene_res.get("findScene"):
        log.error(f"Failed to find scene {stash_scene_id} in Stash")
        return

    existing_markers = scene_res["findScene"].get("scene_markers") or []
    existing_seconds = {m["seconds"] for m in existing_markers}

    created_count = 0
    skipped_count = 0

    for vt in video_tags:
        tag_obj = vt.get("tag") or {}
        title = tag_obj.get("tag_name")
        if not title:
            continue
        title = title.strip()
        seconds = float(vt.get("tag_time", 0))

        tag_id = tags_map.get(title.lower())
        if not tag_id:
            if create_missing_tags:
                log.info(f"Creating missing tag in Stash: '{title}'")
                tag_create_query = """
                mutation TagCreate($input: TagCreateInput!) {
                    tagCreate(input: $input) {
                        id
                        name
                    }
                }
                """
                tag_res = graphql.callGraphQL(
                    tag_create_query, {"input": {"name": title}}
                )
                if tag_res and tag_res.get("tagCreate"):
                    tag_id = tag_res["tagCreate"]["id"]
                    tags_map[title.lower()] = tag_id
                else:
                    log.error(f"Failed to create tag '{title}' in Stash")
                    skipped_count += 1
                    continue
            else:
                log.info(
                    f"Skipping marker '{title}' - tag does not exist in Stash"
                )
                skipped_count += 1
                continue

        # Check for duplicate at timestamp (within 0.05 seconds)
        if any(abs(existing_sec - seconds) < 0.05 for existing_sec in existing_seconds):
            log.debug(
                f"Skipping marker '{title}' at {seconds:.1f}s - marker already exists at this timestamp"
            )
            skipped_count += 1
            continue

        marker_mutation = """
        mutation SceneMarkerCreate($input: SceneMarkerCreateInput!) {
            sceneMarkerCreate(input: $input) {
                id
            }
        }
        """
        marker_variables = {
            "input": {
                "title": title,
                "primary_tag_id": tag_id,
                "seconds": seconds,
                "scene_id": stash_scene_id,
                "tag_ids": [],
            }
        }
        res = graphql.callGraphQL(marker_mutation, marker_variables)
        if res and res.get("sceneMarkerCreate"):
            created_count += 1
            existing_seconds.add(seconds)
        else:
            log.error(f"Failed to create marker '{title}' at {seconds:.1f}s")
            skipped_count += 1

    log.info(
        f"Marker creation complete: {created_count} created, {skipped_count} skipped"
    )


def scrape_scene_data(urls: list[str], stash_scene_id: str | None = None) -> ScrapedScene:
    for url in urls:
        scene_id = extract_scene_id(url)
        if not scene_id:
            log.debug(f"Could not extract scene ID from URL: {url}")
        else:
            break

    if scene_id:
        log.debug(f"Found NVG scene ID {scene_id} from URLs {urls}")
    else:
        log.error("No scene ID found in any URL")
        return {}

    scene_data = fetch_scene_json(scene_id)
    if not scene_data:
        return {}

    title = scene_data.get("short_title")
    if not title:
        log.error(f"No short_title found in API response for scene {scene_id}")
        return {}

    scene: ScrapedScene = {
        "title": title,
        "code": str(scene_data.get("id") or scene_id),
        "urls": [f"https://vip.netvideogirls.com/members/video/{scene_id}"],
    }

    # Studio
    site = scene_data.get("site") or {}
    site_name = site.get("site_name")
    if site_name:
        scene["studio"] = {"name": site_name}

    # Release Date
    date_str = parse_release_date(scene_data.get("release_date"))
    if date_str:
        scene["date"] = date_str

    # Duration
    duration = scene_data.get("video_duration")
    if duration is not None:
        try:
            scene["duration"] = int(duration)
        except (ValueError, TypeError):
            pass

    # Image
    cover_image = get_cover_image(scene_data)
    if cover_image:
        scene["image"] = cover_image

    # Performers
    performers = get_performers(scene_data)
    if performers:
        scene["performers"] = performers

    # Tags
    tags = get_tags(scene_data)
    if tags:
        scene["tags"] = tags

    # Markers
    create_markers_config = bool(config.config_dict.get("CREATE_MARKERS"))
    if create_markers_config:
        if stash_scene_id:
            log.debug(f"Creating markers for Stash scene {stash_scene_id}")
            create_markers(stash_scene_id, scene_data)
        else:
            log.debug(
                "CREATE_MARKERS is enabled but no Stash scene ID was provided "
                "(this is expected if using sceneByUrl; needs sceneByFragment)"
            )

    return scene


if __name__ == "__main__":
    op, args = scraper_args()
    result = None

    log.debug(args)

    if op == "scene-by-url":
        url = args.get("url")
        if url:
            result = scrape_scene_data([url], None)

    if op == "scene-by-fragment":
        urls = args.get("urls")
        if not urls and args.get("url"):
            urls = [args.get("url")]
        if urls:
            stash_scene_id = args.get("id")
            result = scrape_scene_data(urls, stash_scene_id)

    if result:
        print(json.dumps(result))
    else:
        print(json.dumps({}))
