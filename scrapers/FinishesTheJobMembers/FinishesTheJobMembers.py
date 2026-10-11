import base64
from datetime import datetime
import json
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from py_common.deps import ensure_requirements
ensure_requirements("lxml")

from lxml import html

from py_common.config import get_config
import py_common.log as log
from py_common.proxy import StashRequests
from py_common.types import ScrapedPerformer, ScrapedScene, ScrapedTag
from py_common.util import scraper_args

requests = StashRequests()

STUDIO_NAMES: dict[str, str] = {
    "finishesthejob": "Finishes The Job",
    "manojob": "Mano Job",
    "mrpov": "Mr. POV",
    "thedicksuckers": "The Dick Suckers",
}

config = get_config(
    # CONFIG_NOTES

    # COOKIES should be set as a semicolon-delimited string of name=value pair(s) without any newlines.
    # You may copy and paste Chrome network tools 'Request' tab "Cookie:" value.
    # e.g., COOKIES = setup1_PSLOGIN=sUsll2; eteens=YY/69aljfoojfg|kljgg/420A==
    # Cookies `l3_s_usr`, `l3_s_val`, and `fabulouscash_llc_members_sites_session`
    # are used. Others are ignored.

    # PREFERRED_DOMAIN should be set to one of: finishesthejob, manojob, thedicksuckers, or mrpov.
    # This is the domain that will actually be used to fetch the scene pages, so should match the
    # site that you are logged in to with your cookies. Because all of the sites allow access to
    # all of the content (AFAIK), this doesn't impact what you can scrape, just whether you
    # can scrape anything released under a different sub-studio than your subscription.

    default="""
    # See CONFIG_NOTES in FinishesTheJobMembers.py for documentation
    COOKIES =
    PREFERRED_DOMAIN =
"""
)


def get_scrapable_url(url: str) -> str:
    preferred_domain = config.config_dict.get("PREFERRED_DOMAIN")
    if not preferred_domain:
        log.warning("No PREFERRED_DOMAIN is set; leaving URL as-is.")
        return url
    target_netloc = f"www.{preferred_domain.strip()}.com"
    split = urlsplit(url)
    return urlunsplit(split._replace(netloc=target_netloc))


def get_cookies_dict() -> dict[str, str]:
    necessary_cookies = {"l3_s_usr", "l3_s_val", "fabulouscash_llc_members_sites_session"}
    val = config.config_dict.get("COOKIES") or ""
    cookies = {}
    for part in str(val).split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            if k in necessary_cookies:
                cookies[k] = v.strip()
                necessary_cookies.discard(k)
    if necessary_cookies:
        log.error(f"Missing necessary cookies: {', '.join(necessary_cookies)}")
        return {}

    return cookies


def fetch_html_tree(url: str) -> html.HtmlElement | None:
    """Fetches HTML content from a URL and returns the parsed lxml HTML tree,
    with relative links converted to absolute.
    """
    cookies = get_cookies_dict()
    if not cookies:
        log.error("Exiting due to missing cookies")
        return None

    log.debug(f"Fetching URL: {url}")

    try:
        response = requests.get(url, cookies=cookies, timeout=10)
        response.raise_for_status()
    except Exception as e:
        log.error(f"Failed to fetch URL '{url}': {e}")
        return None

    tree = html.fromstring(response.content, base_url=url)
    return tree


def get_scene_title(tree: html.HtmlElement) -> str | None:
    return tree.xpath("string(//h1)").strip() or None


def get_release_date(tree: html.HtmlElement) -> str | None:
    """Extracts and standardizes the release date in YYYY-MM-DD format."""
    for element in tree.xpath("//strong[contains(@class, 'text-muted')]"):
        text = element.text_content()
        log.info(f"Date text: {text}")
        # Example date strings: `Jun 5th, 2026`, `May 23rd, 2014`
        matches = re.search(r'([A-Za-z]{3}) (\d{1,2})[a-z]{2}, (\d{4})', text)
        if matches:
            month, day, year = matches.groups()
            try:
                dt = datetime.strptime(f"{month} {day} {year}", "%b %d %Y")
                log.debug(f"Parsed release date: {dt.strftime('%Y-%m-%d')}")
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                pass
    return None


def get_description(tree: html.HtmlElement) -> str | None:
    # This is brittle a.f. If it proves to be problematic, consider looking
    # for the last <p> before the comments section.
    return tree.xpath("string(//p)").strip() or None


def get_performers(tree: html.HtmlElement) -> list[ScrapedPerformer]:
    performer_anchors = tree.xpath("//span[contains(@class, 'performers')]//a")
    performers: list[ScrapedPerformer] = []
    for anchor in performer_anchors:
        name = anchor.text_content().strip()
        performers.append({"name": name})
    return performers


def get_tags(tree: html.HtmlElement) -> list[ScrapedTag]:
    tag_anchors = tree.xpath("//span[contains(@class, 'categories')]//a")
    tags: list[ScrapedTag] = []
    for anchor in tag_anchors:
        name = anchor.text_content().strip()
        tags.append({"name": name})
    return tags


def get_image(tree: html.HtmlElement) -> str | None:
    # Loading image requires cookies, so we'll fetch it now with the cookies we have
    # and return the full image file as a base64 encoded data URI so that the client
    # doesn't need cookies to load the image.
    image_url = tree.xpath("string(//video//@poster)")
    if not image_url:
        log.warning("No video poster URL found")
        return None
    absolute_image_url = urljoin(tree.base_url, image_url)

    try:
        response = requests.get(absolute_image_url, cookies=get_cookies_dict(), timeout=10)
        response.raise_for_status()
    except Exception as e:
        log.error(f"Failed to fetch image URL '{absolute_image_url}': {e}")
        return None

    base64_string = base64.b64encode(response.content).decode("utf-8")
    content_type = response.headers.get("Content-Type", "image/jpeg")
    mime_type = content_type.split(";")[0].strip() or "image/jpeg"
    data_url = f"data:{mime_type};base64,{base64_string}"

    return data_url


def get_studio_name(tree: html.HtmlElement, url: str) -> tuple[str, str] | None:
    """
    Returns a tuple of (studio name, studio slug).
    """
    studio_slug = url.split("/")[-2]
    studio_name = STUDIO_NAMES.get(studio_slug, "")
    if not studio_name:
        studio_name = tree.xpath("string(//h2)")
    return (studio_name, studio_slug)


def check_public_url_validity(public_url: str) -> bool:
    """Returns True iff the public URL returns a 200 status code."""
    try:
        # We disallow redirects because a hypothetical public URL for a scene which is not publicly
        # visible redirects (302) to the tour index page.
        response = requests.head(public_url, allow_redirects=False, timeout=10)
        if response.status_code == 200:
            log.debug(f"Public URL is valid (200): {public_url}")
            return True
        log.debug(f"Public URL is invalid (status {response.status_code}): {public_url}")
        return False
    except Exception as e:
        log.warning(f"Error checking public URL {public_url}: {e}")
        return False


def get_urls(url: str, studio_slug: str) -> list[str]:
    """Builds canonical members URL and probes for an active public scene URL."""
    urls: list[str] = []
    parsed = urlsplit(url)
    path_parts = [p for p in parsed.path.split("/") if p]
    scene_slug = path_parts[-1] if path_parts else ""

    # Canonical members' URL:
    if studio_slug:
        target_netloc = f"www.{studio_slug}.com"
        urls.append(urlunsplit(parsed._replace(netloc=target_netloc)))
    else:
        urls.append(url)

    # Check whether there's a public URL available
    if studio_slug and scene_slug:
        hypothetical_public_url = (
            f"https://www.{studio_slug}.com/scene/{studio_slug}/{scene_slug}"
        )
        if check_public_url_validity(hypothetical_public_url):
            urls.append(hypothetical_public_url)
        else:
            log.info(f"Public URL is not valid: {hypothetical_public_url} (this is expected to be the case sometimes)")

    return urls


def scrape_scene_data(url: str) -> ScrapedScene:
    """Scrapes metadata for the scene at the URL.

    Args:
        url: The scene URL.

    Returns:
        A dictionary of scraped scene metadata.
    """
    log.debug(f"Scraping scene URL: {url}")

    url = get_scrapable_url(url)
    log.debug(f"Using scrapable URL: {url}")

    tree = fetch_html_tree(url)
    if tree is None:
        return {}

    scene: ScrapedScene = {}

    title_text = get_scene_title(tree)
    if title_text:
        scene["title"] = title_text
    else:
        log.error("Unable to find title. Exiting")
        return {}

    date_str = get_release_date(tree)
    if date_str:
        scene["date"] = date_str

    description = get_description(tree)
    if description:
        scene["details"] = description

    performers = get_performers(tree)
    if performers:
        scene["performers"] = performers

    scene["tags"] = get_tags(tree)

    image_url = get_image(tree)
    if image_url:
        scene["image"] = image_url

    studio_name, studio_slug = get_studio_name(tree, url)
    scene["studio"] = {"name": studio_name}
    log.debug(f"Studio: {studio_name}, Slug: {studio_slug}")

    scene["urls"] = get_urls(url, studio_slug)
    if all("/members/" in u for u in scene["urls"]):
        scene["tags"].append({"name": "Members Only"})

    return scene


if __name__ == "__main__":
    op, args = scraper_args()
    result = None

    if op == "scene-by-url":
        url = args.get("url")
        if url:
            result = scrape_scene_data(url)

    print(json.dumps(result or {}))