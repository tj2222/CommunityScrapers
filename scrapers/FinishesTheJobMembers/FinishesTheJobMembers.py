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


class FTJMUrl:
    # "Finishes The Job Members URL"

    URL_PATTERN = re.compile(r"https?://(?:www\.)?(?P<domain>[^.]+)\.[^/]+/members/scene/(?P<scene_number>\d+)/(?P<studio_slug>[^/]+)/(?P<scene_slug>[^/?#]+)(?:#(?P<fragment>[^#]+))?")
    
    def __init__(self, url: str) -> None:
        match = self.URL_PATTERN.search(url)
        if not match:
            log.error(f"Failed to parse members URL: {url}")
            raise ValueError(f"Failed to parse members URL: {url}")
        # self.domain = match.group("domain")#
        self._scene_number = match.group("scene_number")
        self._studio_slug = match.group("studio_slug")
        self._scene_slug = match.group("scene_slug")
        self._fragment = match.group("fragment")
    
    def to_canonical(self) -> str:
        return f"https://www.{self._studio_slug}.com/members/scene/{self._scene_number}/{self._studio_slug}/{self._scene_slug}"

    def with_forced_domain(self, domain: str) -> str:
        return f"https://www.{domain}.com/members/scene/{self._scene_number}/{self._studio_slug}/{self._scene_slug}"

    def to_public(self) -> str:
        return f"https://www.{self._studio_slug}.com/scene/{self._studio_slug}/{self._scene_slug}"

    def get_studio_slug(self) -> str:
        return self._studio_slug

    def get_fragment(self) -> str:
        return self._fragment

STUDIO_NAMES: dict[str, str] = {
    "finishesthejob": "Finishes The Job",
    "manojob": "Mano Job",
    "mrpov": "Mr. POV",
    "secretstash": "Secret Stash",
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


def get_preferred_domain() -> str:
    return config.config_dict.get("PREFERRED_DOMAIN")


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


def get_studio_name(tree: html.HtmlElement, studio_slug: str) -> str:
    studio_name = STUDIO_NAMES.get(studio_slug, "")
    if not studio_name:
        log.warning(f"No studio name found for slug '{studio_slug}'.")
        studio_name = tree.xpath("string(//h2)").strip()
    return studio_name


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


def get_urls(url: FTJMUrl) -> list[str]:
    urls: list[str] = [url.to_canonical()]
    if check_public_url_validity(url.to_public()):
        urls.append(url.to_public())
    else:
        log.info(f"Public URL is not valid: {url.to_public()} (this is expected to be the case sometimes)")
    return urls


def get_extra_video_label(tree: html.HtmlElement, fragment: str) -> str | None:
    label_regex: str | None = None
    if fragment == "bts":
        label_regex = r"behind the scenes"
    elif fragment == "interview":
        label_regex = r"interview"
    elif fragment == "fly":
        label_regex = r"fly"
    elif fragment == "social":
        label_regex = r"social"
    elif fragment == "pop":
        label_regex = r"cum.?shot"
    else:
        log.warning(f"Unknown fragment: {fragment}")
        return None
    
    if label_regex:
        source_anchors = tree.xpath("//a[contains(@class, 'changeSource')]")
        for anchor in source_anchors:
            text = anchor.text_content().strip()
            if re.search(label_regex, text, re.IGNORECASE):
                return text

    log.warning(f"Could not find matching label for fragment '{fragment}' by regex '{label_regex}'")
    return None    


def scrape_scene_data(input_url: str) -> ScrapedScene:
    """Scrapes metadata for the scene at the URL.

    Args:
        input_url: The scene URL.

    Returns:
        A dictionary of scraped scene metadata.
    """
    log.debug(f"Scraping scene URL: {input_url}")

    try:
        url = FTJMUrl(input_url)
    except:
        log.error(f"Unable to parse URL: {input_url}")
        return {}

    preferred_domain = get_preferred_domain()
    if not preferred_domain:
        log.warning("No PREFERRED_DOMAIN is set; using studio slug.")
        preferred_domain = url.get_studio_slug()
    tree = fetch_html_tree(url.with_forced_domain(preferred_domain))
    if tree is None:
        return {}

    scene: ScrapedScene = {}
    scene["tags"] = []

    extra_video_label = get_extra_video_label(tree, url.get_fragment())
    log.debug(f"Extra video label: {extra_video_label}")

    title_text = get_scene_title(tree)
    if extra_video_label and title_text:
        title_text += f" - {extra_video_label}"
    if title_text:
        scene["title"] = title_text
    else:
        log.error("Unable to find title. Exiting")
        return {}

    date_str = get_release_date(tree)
    if date_str:
        scene["date"] = date_str

    performers = get_performers(tree)
    if performers:
        scene["performers"] = performers

    studio_name = get_studio_name(tree, url.get_studio_slug())
    scene["studio"] = {"name": studio_name}

    scene["urls"] = get_urls(url)
    if all("/members/" in u for u in scene["urls"]):
        scene["tags"].append({"name": "Members Only"})

    if not extra_video_label:        
        description = get_description(tree)
        if description:
            scene["details"] = description

        scene["tags"].extend(get_tags(tree))

        image_url = get_image(tree)
        if image_url:
            scene["image"] = image_url

    return scene


if __name__ == "__main__":
    op, args = scraper_args()
    result = None

    if op == "scene-by-url":
        url = args.get("url")
        if url:
            result = scrape_scene_data(url)

    print(json.dumps(result or {}))