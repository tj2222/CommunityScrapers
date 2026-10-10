from py_common.config import get_config
from py_common.util import scraper_args
from py_common.proxy import StashRequests
from py_common.types import ScrapedScene, ScrapedTag, ScrapedPerformer
from lxml import html
import py_common.log as log
import base64
import json
import re
from datetime import datetime
from urllib.parse import urljoin

from py_common.deps import ensure_requirements
ensure_requirements("lxml")

requests = StashRequests()



config = get_config(
    # CONFIG_NOTES

    # Cookies should be set as a semicolon-delimited string of name=value pair(s) without any newlines.
    # You may copy and paste Chrome network tools 'Request' tab "Cookie:" value.
    # e.g., COOKIES = setup1_PSLOGIN=sUsll2; eteens=YY/69aljfoojfg|kljgg/420A==
    # Cookies `l3_s_usr`, `l3_s_val`, and `fabulouscash_llc_members_sites_session`
    # are used. Others are ignored.

    default="""
    # See CONFIG_NOTES in FinishesTheJobMembers.py for documentation
    COOKIES =
"""
)


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
    """Fetches HTML content from a URL and returns the parsed
       lxml HTML tree, with relative links converted to absolute."""
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
    # Example date strings: `Jun 5th, 2026`, `May 23rd, 2014`
    for element in tree.xpath("//strong[contains(@class, 'text-muted')]"):
        text = element.text_content()
        log.info(f"Date text: {text}")
        matches = re.search(r'([A-Za-z]{3}) (\d{1,2})[a-z]{2}, (\d{4})', text)
        if matches:
            month, day, year = matches.groups()
            try:
                dt = datetime.strptime(f"{month} {day} {year}", "%b %d %Y")
                log.info(f"Date: {dt.strftime('%Y-%m-%d')}")
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
    absolute_image_url = urljoin(tree.base_url, image_url)

    try:
        response = requests.get(absolute_image_url, cookies=get_cookies_dict(), timeout=10)
        response.raise_for_status()
    except Exception as e:
        log.error(f"Failed to fetch image URL '{image_url}': {e}")
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
    studio_name = tree.xpath("string(//h2)")
    studio_slug = url.split("/")[-2]
    return (studio_name, studio_slug)


def get_urls(url: str, studio_name: str) -> list[str]:
    pass


def get_studio_code(url: str) -> str:
    return "TODO"


def scrape_scene_data(url: str) -> ScrapedScene:
    """Scrapes metadata for the scene at the URL.

    Args:
        url: The scene URL.

    Returns:
        A dictionary of scraped scene metadata.
    """
    log.debug(f"Scraping scene URL: {url}")

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
    # TODO: Check whether public page is available; add Members Only tag if not.

    image_url = get_image(tree)
    if image_url:
        scene["image"] = image_url

    studio_name, studio_slug = get_studio_name(tree, url)
    scene["studio"] = {"name": studio_name}
    log.info(f"Studio: {studio_name}, Slug: {studio_slug}")

    scene["code"] = get_studio_code(tree)

    # TODO include public URL if available
    scene["urls"] = get_urls(url, studio_name)

    return scene


if __name__ == "__main__":
    op, args = scraper_args()
    result = None

    if op == "scene-by-url":
        url = args.get("url")
        if url:
            result = scrape_scene_data(url)

    if result:
        print(json.dumps(result))
    else:
        print(json.dumps({}))