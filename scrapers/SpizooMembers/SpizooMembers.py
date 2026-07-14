import json
import re
from datetime import datetime
from os.path import basename, commonprefix
from urllib.parse import urljoin, urlparse

from py_common.deps import ensure_requirements
ensure_requirements("lxml")

import py_common.log as log
from lxml import html
from py_common.proxy import StashRequests
from py_common.util import scraper_args
from py_common.config import get_config

# Scrapes from /members and /expired URLs on Spizoo network sites. Must set cookies in config.ini (see below)
# Even after a membership expires, Spizoo sites allow the user to access logged-in information via an /expired path.
# Benefits over logged-out scraping:
# + Scrapes tags from all sites. Some sites do not show tags on logged-out scene pages.
# + Scrapes male performers, only visible on logged-in version of tagteampov.com (and perhaps other sites).
# + Scrapes scenes that are not visible to logged-out users.


config = get_config(
    # CONFIG_NOTES
    # Set your member auth cookies here for each site to scrape member content.

    # When using cookies for **active** accounts:
    # Cookies `PHPSESSID`, `LBSERVERID`, and `pcar%5fTWVtYmVycyBBcmVh` are used. Others are ignored.
    # Don't set SITEXYZ_IS_EXPIRED to true or 1. Can omit it or leave it blank or set it to false.

    # When using cookies for **expired** accounts:
    # Cookie `pcar%5fTWVtYmVycyBBcmVh` is used. Others are ignored.
    # Set SITEXYZ_IS_EXPIRED to true or 1.

    # Cookies should be set as a semicolon-delimited string of name=value pair(s) without any newlines.
    # You may copy and paste Chrome network tools 'Request' tab "Cookie:" value.
    # e.g., MRLUCKYVIP_COOKIES =PHPSESSID=c6a245halnn; LBSERVERID=dd7299; pcar%5fTWVtYmVycyBBcmVh=RW...0K
    # or MRLUCKYVIP_COOKIES=pcar%5fTWVtYmVycyBBcmVh=RW...0K
    # Set SITEXYZ_IS_EXPIRED = true if your cookie for that site is for an expired membership.
    default="""
    # See CONFIG_NOTES in SpizooMembers.py for documentation
    CREAMHER_COOKIES =
    CREAMHER_IS_EXPIRED =
    DRDADDYPOV_COOKIES =
    DRDADDYPOV_IS_EXPIRED =
    FIRSTCLASSPOV_COOKIES =
    FIRSTCLASSPOV_IS_EXPIRED =
    GOTHGIRLFRIENDS_COOKIES =
    GOTHGIRLFRIENDS_IS_EXPIRED =
    GOTHGIRLFRIENDSVIP_COOKIES =
    GOTHGIRLFRIENDSVIP_IS_EXPIRED =
    MRLUCKYLIFE_COOKIES =
    MRLUCKYLIFE_IS_EXPIRED =
    MRLUCKYPOV_COOKIES =
    MRLUCKYPOV_IS_EXPIRED =
    MRLUCKYRAW_COOKIES =
    MRLUCKYRAW_IS_EXPIRED =
    MRLUCKYVIP_COOKIES =
    MRLUCKYVIP_IS_EXPIRED =
    RAWATTACK_COOKIES =
    RAWATTACK_IS_EXPIRED =
    SPIZOO_COOKIES =
    SPIZOO_IS_EXPIRED =
    TAGTEAMPOV_COOKIES =
    TAGTEAMPOV_IS_EXPIRED =
    VLOGXXX_COOKIES =
    VLOGXXX_IS_EXPIRED =
"""
)

requests = StashRequests()


def get_site_name(domain: str) -> str:
    """Extracts the base site/studio name from a domain.

    Args:
        domain: The domain string (e.g., 'www.creamher.com' or 'members.spizoo.com').

    Returns:
        The base site name (e.g., 'creamher' or 'spizoo').
    """
    domain = domain.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    parts = domain.split(".")
    return parts[-2] if len(parts) >= 2 else parts[0]


def get_cookies_dict(domain: str, expired: bool = False) -> dict:
    """Retrieves and prepares the session cookie dictionary for a domain.

    Args:
        domain: The site domain.
        expired: Whether the request will use /expired/ instead of /members/.

    Returns:
        A dictionary with only the necessary cookie keys and values, or an empty dict.
    """
    site_key = get_site_name(domain).upper()
    val = config.config_dict.get(f"{site_key}_COOKIES") or config.config_dict.get(f"{site_key}_PCAR")
    if not val:
        return {}

    #Strip double quotes in case user wrapped the value in them.
    val = str(val).strip().strip("\"'")
    cookies = {}
    for part in val.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip().strip("\"'")

    # Filter only necessary cookies based on page type
    filtered_cookies = {}
    for k, v in cookies.items():
        k_lower = k.lower()
        if k_lower.startswith("pcar"):
            filtered_cookies[k] = v
        elif not expired and k in ("PHPSESSID", "LBSERVERID"):
            # When not an expired membership, these cookies are necessary in order to prevent a redirect to a deals/upsell page.
            filtered_cookies[k] = v

    return filtered_cookies


def is_site_expired(domain: str) -> bool:
    """Checks if the membership for the given domain is expired based on config.

    Args:
        domain: The site domain.

    Returns:
        True if the membership is configured as expired, False otherwise.
    """
    site_key = get_site_name(domain).upper()
    key = f"{site_key}_IS_EXPIRED"
    try:
        val = config[key]
    except KeyError:
        return False

    if not val:
        return False

    val_str = str(val).strip().lower()
    return val_str in ("1", "true")


def get_url_for_scraping(url: str, expired: bool) -> str:
    """Rewrites the URL path to convert it to the expected scraping URL path.

    If a public/updates URL is input, it converts it to the members or expired scene URL based on `expired`.
    Otherwise, it replaces /members/scenes/ with /expired/scenes/ (or vice versa) to match the `expired` state.

    Args:
        url: The scene URL.
        expired: Whether the site uses /expired/ instead of /members/.

    Returns:
        The normalized URL for scraping.
    """
    if re.search(r'/updates/', url, re.IGNORECASE):
        path = 'expired' if expired else 'members'
        return re.sub(
            r'/updates/(.*?)\.html',
            rf'/{path}/scenes/\1_vids.html',
            url,
            flags=re.IGNORECASE
        )

    if expired:
        return re.sub(r'/members/scenes/', '/expired/scenes/', url, flags=re.IGNORECASE)
    else:
        return re.sub(r'/expired/scenes/', '/members/scenes/', url, flags=re.IGNORECASE)


def get_presumed_public_url(url: str) -> str:
    """Derives the expected public scene URL from a logged-in members URL.

    Args:
        url: The logged-in member scene URL.

    Returns:
        The public scene URL.
    """
    # Replace members/expired scene path with public updates path using backreference for scene ID
    return re.sub(
        r'/(?:members|expired)/scenes/(.*?)_vids\.html',
        r'/updates/\1.html',
        url,
        flags=re.IGNORECASE
    )


def check_public_url_validity(public_url: str) -> bool:
    """Checks if the derived public scene URL actually exists (returns HTTP 200).

    Args:
        public_url: The public scene URL to check.

    Returns:
        True if valid, False otherwise.
    """
    try:
        response = requests.head(public_url, allow_redirects=False, timeout=5)
        if response.status_code == 200:
            log.debug(f"Public URL is valid (200): {public_url}")
            return True
        else:
            log.debug(f"Public URL is invalid (status {response.status_code}): {public_url}")
            return False
    except Exception as e:
        log.warning(f"Error checking public URL {public_url}: {e}")
        return False


def get_scene_title(tree) -> str:
    title_text = ""
    xpath_expr = "//a[@data-scene]/@data-scene"
    elements = tree.xpath(xpath_expr)
    if elements:
        title_text = elements[0].strip()
        log.debug(f"Found title in data-scene attribute: {title_text}")
    if not title_text:
        title_text = tree.xpath("//title")[0].text_content().strip()
        log.debug(f"Found title in title tag: {title_text}")
    return title_text


def get_release_date(tree) -> str:
    """Extracts the release date from the parsed HTML tree.

    Args:
        tree: The lxml HTML tree.

    Returns:
        The ISO release date string.
    """
    release_date = ""

    # Note that the raw string may include content such as "Release date:"
    # Only date formats seen so far: MM/DD/YYYY and YYYY-MM-DD
    xpath_expr = "//*[@class='date']"
    elements = tree.xpath(xpath_expr)

    if elements:
        raw_date_str = elements[0].text_content().strip()
        log.debug(f"Found raw date string: {raw_date_str}")
        slash_format = re.search(r"\d{2}/\d{2}/\d{4}", raw_date_str)
        if slash_format:
            try:
                dt = datetime.strptime(slash_format.group(0), "%m/%d/%Y")
                release_date = dt.strftime("%Y-%m-%d")
            except ValueError:
                log.error(f"Failed to parse date: {raw_date_str}")
        else:
            dash_format = re.search(r"\d{4}-\d{2}-\d{2}", raw_date_str)
            if dash_format:
                try:
                    dt = datetime.strptime(dash_format.group(0), "%Y-%m-%d")
                    release_date = dt.strftime("%Y-%m-%d")
                except ValueError:
                    log.error(f"Failed to parse date: {raw_date_str}")
    if release_date:
        log.debug(f"Parsed release date: {release_date}")
    return release_date


def get_details(tree) -> str:
    """Extracts and cleans the scene description/details from the parsed HTML tree.

    Args:
        tree: The lxml HTML tree.

    Returns:
        The details text.
    """
    details = ""
    xpath_expr = "//p[@class='description']"
    elements = tree.xpath(xpath_expr)
    if elements:
        details = elements[0].text_content().strip()
    if details:
        log.debug(f"Found details: {details}")
    else:
        log.debug("No details found")
    return details


def get_performers(tree) -> list:
    """Extracts the performers of the scene from the parsed HTML tree.

    Args:
        tree: The lxml HTML tree.

    Returns:
        A list of performer dictionaries containing performer name.
    """
    xpath_expr = "//a[@class='model-name']/@title"
    perf_titles = tree.xpath(xpath_expr)
    performers = []
    # Performer list is sometimes repeated on the same page.
    seen = set()
    for title in perf_titles:
        name = title.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            performers.append({"name": name})
    if performers:
        log.debug(f"Found performers: {performers}")
    else:
        log.warning("No performers found")
    return performers


def get_tags(tree) -> list:
    """Extracts the tags/categories of the scene from the parsed HTML tree.

    Args:
        tree: The lxml HTML tree.

    Returns:
        A list of tag dictionaries containing tag name.
    """
    xpath_expr = "//a[contains(@href,'/categories')]//@title"
    tag_titles = tree.xpath(xpath_expr)
    tags = []
    # Tag list is sometimes repeated on the same page
    seen = set()
    for title in tag_titles:
        name = title.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            tags.append({"name": name})
    if tags:
        log.debug(f"Found tags: {tags}")
    else:
        log.warning("No tags found")
    return tags


def get_image(tree) -> str:
    """Extracts the poster or preview image URL from the parsed HTML tree.

    Args:
        tree: The lxml HTML tree.

    Returns:
        The absolute image URL, or None.
    """
    xpath_expr = "//video/@poster"
    images = tree.xpath(xpath_expr)
    if images:
        img_url = images[0].strip()
        img_url = re.sub(r"[?&]img(?:q|w|h)=[^&]+", "", img_url)
        return img_url
    return None


def get_studio(tree, url: str) -> dict:
    """Determines the studio name from the parsed HTML tree and page URL.

    Args:
        tree: The lxml HTML tree.
        url: The page URL.

    Returns:
        A dictionary containing the studio name.
    """
    site_el = tree.xpath("//i[@id='site']/@value")
    site_val = site_el[0].strip() if site_el else ""
    
    if not site_val:
        base_el = tree.xpath("//base/@href")
        site_val = base_el[0].strip() if base_el else ""
        
    if not site_val:
        site_val = url
        
    domain = urlparse(site_val).netloc.lower() or site_val.lower()
    site_key = get_site_name(domain)
    
    studio_map = {
        "creamher": "Cream Her",
        "drdaddypov": "Dr. Daddy POV",
        "firstclasspov": "First Class POV",
        "gothgirlfriends": "Goth Girlfriends",
        "gothgirlfriendsvip": "Goth GirlfriendsVIP",
        "mrluckylife": "Mr. LuckyLIFE",
        "mrluckypov": "Mr. LuckyPOV",
        "mrluckyraw": "Mr. LuckyRaw",
        "mrluckyvip": "Mr. LuckyVIP",
        "rawattack": "RawAttack",
        "spizoo": "Spizoo",
        "tagteampov": "Tag Team POV",
        "vlogxxx": "Vlog XXX"
    }
    
    studio_name = studio_map.get(site_key, site_key.capitalize())
    return {"name": studio_name}


def scrape_scene_data(url: str) -> dict:
    """Scrapes metadata for a given member scene URL.

    Args:
        url: The scene URL.

    Returns:
        A dictionary of scraped scene metadata.
    """
    domain = urlparse(url).netloc.lower()
    expired = is_site_expired(domain)
    url = get_url_for_scraping(url, expired)

    log.debug(f"Scraping URL: {url}")
    
    cookies = get_cookies_dict(domain, expired)

    # if not cookies:
    #     presumed_public_url = get_presumed_public_url(url)
    #     if check_public_url_validity(presumed_public_url):
    #         log.warning(f"No cookie found for URL {url} . Returning public URL for the user scrape: {presumed_public_url}")
    #         return {"urls": [presumed_public_url]}
    #     else:
    #         log.warning(f"No cookie found for URL {url}. Unable to find working public URL. Returning Members Only tag.")
    #         wayback_url = f"https://web.archive.org/web/*/{presumed_public_url}"
    #         log.debug(f"Scene would hypothetically have public URL of {presumed_public_url}, but it is not valid. You may try checking the Wayback Machine at {wayback_url}.")
    #         return {"tags": [{"name": "Members Only"}]}
    
    log.debug(f"Using cookie authentication (sending {len(cookies)} cookies)")

    try:
        response = requests.get(url, cookies=cookies, timeout=10)
        response.raise_for_status()
    except Exception as e:
        log.error(f"Failed to fetch URL {url}: {e}")
        return {}

    tree = html.fromstring(response.content)
    scene = {}

    canonical_links = tree.xpath("//link[@rel='canonical']/@href")
    scene_url = canonical_links[0].strip() if canonical_links else url
    scene_url = re.sub(r'/expired/scenes/', '/members/scenes/', scene_url, flags=re.IGNORECASE)

    presumed_public_url = get_presumed_public_url(scene_url)
    public_url_is_valid = check_public_url_validity(presumed_public_url)

    is_vip_scene = False
    title_text = get_scene_title(tree)
    if title_text:
        scene["title"] = title_text
        if re.search(r"\bVIP\d?\b", title_text):
            is_vip_scene = True

    date_str = get_release_date(tree)
    if date_str:
        scene["date"] = date_str

    details = get_details(tree)
    if details:
        scene["details"] = details

    performers = get_performers(tree)
    if performers:
        scene["performers"] = performers

    scene["tags"] = get_tags(tree)
    if is_vip_scene:
        scene["tags"].append({"name": "Bonus Scenes"})
    if not public_url_is_valid:
        scene["tags"].append({"name": "Members Only"})
    if expired:
        scene["tags"].append({"name": "Scraped with expired membership"})

    image_url = get_image(tree)
    if image_url:
        scene["image"] = image_url

    scene["studio"] = get_studio(tree, url)

    scene["urls"] = [scene_url]
    if public_url_is_valid:
        scene["urls"].append(presumed_public_url)
    else:
        wayback_url = f"https://web.archive.org/web/*/{presumed_public_url}"
        log.debug(f"Scene would hypothetically have public URL of {presumed_public_url}, but it is not valid. You may try checking the Wayback Machine at {wayback_url}.")

    return scene


if __name__ == "__main__":
    op, args = scraper_args()
    result = None

    if op in ["scene-by-url", "scene-by-query-fragment"]:
        url = args.get("url")
        if url:
            result = scrape_scene_data(url)

    if result:
        print(json.dumps(result))
    else:
        print(json.dumps({}))
