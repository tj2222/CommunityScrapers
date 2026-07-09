import json
import re
from datetime import datetime, timedelta
from os.path import basename, commonprefix
from urllib.parse import urljoin, urlparse

from py_common.deps import ensure_requirements
ensure_requirements("lxml")

import py_common.log as log
from lxml import html
from py_common.proxy import StashRequests
from py_common.util import scraper_args
from py_common.config import get_config

# Scrapes from /members and /expired URLs on Spizoo network sites
# Benefits over logged-out scraping:
# + Scrapes tags from all sites. Some do not show tags on logged-out scene pages.
# + Scrapes male performers, not visible on logged-out version of tagteampov.com (and perhaps other sites).
# + Scrapes scenes that are no longer visible to logged-out users.




config = get_config(
    # CONFIG_NOTES
    # Set your member auth cookies (pcar...) here for each site to scrape member content.
    # Can be the full cookie (name=value) or just the cookie value itself.
    # e.g., CREAMHER_PCAR = pcar%5fTWVtYmVycyBBcmVh=RW..0K or CREAMHER_PCAR = RW...0K
    default="""
    CREAMHER_PCAR =
    DRDADDYPOV_PCAR =
    FIRSTCLASSPOV_PCAR =
    GOTHGIRLFRIENDS_PCAR =
    GOTHGIRLFRIENDSVIP_PCAR =
    MRLUCKYLIFE_PCAR =
    MRLUCKYPOV_PCAR =
    MRLUCKYRAW_PCAR =
    MRLUCKYVIP_PCAR =
    RAWATTACK_PCAR =
    SPIZOO_PCAR =
    TAGTEAMPOV_PCAR =
    VLOGXXX_PCAR =
"""
)

requests = StashRequests()


def get_site_name(domain: str) -> str:
    # e.g., www.mrluckylife.com -> mrluckylife
    domain = domain.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    parts = domain.split(".")
    return parts[-2] if len(parts) >= 2 else parts[0]


def get_pcar_cookies_dict(domain: str) -> dict:
    site_key = get_site_name(domain).upper()
    key = f"{site_key}_PCAR"

    val = config.get(key, "")
    if not val:
        return {}

    val = str(val).strip().strip("\"'")
    cookies = {}
    if "=" in val:
        parts = val.split("=", 1)
        if parts[0].strip().lower().startswith("pcar"):
            cookies[parts[0].strip()] = parts[1].strip().strip("\"'")
            return cookies

    cookies["pcar%5fTWVtYmVycyBBcmVh"] = val
    return cookies


def get_presumed_public_url(url: str) -> str:
    # Match either _vids.html or just .html under /members/scenes/
    match = re.search(r'/members/scenes/(.*?)(?:_vids)?\.html', url, re.IGNORECASE)
    if match:
        scene_id = match.group(1)
        url = re.sub(r'/members/scenes/.*\.html', f'/updates/{scene_id}.html', url, flags=re.IGNORECASE)
    return url


def check_public_url_validity(public_url: str) -> bool:
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


def get_title_text(tree) -> str:
    xpath_exprs = [
        "//div[@class='title' or @class='row']//h1",
        "//div[@class='title-trailer']//h2",
        "//h2[contains(@class, 'titular')]",
        "//div[@class='objectInfo']/h1",
        "//title"
    ]
    for expr in xpath_exprs:
        elements = tree.xpath(expr)
        if elements:
            title_text = elements[0].text_content().strip()
            if title_text:
                title_text = re.sub(r'^Tour\s\-\s*', '', title_text, flags=re.IGNORECASE)
                title_text = re.sub(r'\s\-\s*$', '', title_text, flags=re.IGNORECASE)
                return title_text.strip()
    return ""


def parse_date_string(date_text: str) -> str:
    """Extract date from string like "2026-07-04" or "March 1, 2026" or similar."""
    if not date_text:
        return ""
    m_ymd = re.search(r'(\d{4})-(\d{2})-(\d{2})', date_text)
    if m_ymd:
        return m_ymd.group(0)
        
    m = re.search(r'([A-Za-z]+)\.?\s*(\d{1,2}),\s*(\d{4})', date_text)
    if m:
        month_str, day_str, year_str = m.groups()
        month_str = month_str[:3].title()
        try:
            dt = datetime.strptime(f"{month_str} {day_str} {year_str}", "%b %d %Y")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def get_release_date(tree) -> tuple[str, bool]:
    release_date = ""
    earliest_comment_date = ""
    is_estimate = False

    # Spizoo members date selector
    xpath_expr = (
        "(//section[@id='trailer-data' or @id='sceneInfo' or @id='scene-info' or @id='des-scene'])"
        "//p[@class='date']"
    )
    elements = tree.xpath(xpath_expr)
    if not elements:
        elements = tree.xpath("//div[@class='objectInfo']//p[contains(., 'Released')]/span")
        
    if elements:
        date_str = parse_date_string(elements[0].text_content().strip())
        if date_str:
            log.debug(f"Found explicit release date: {date_str}")
            release_date = date_str

    # Extract earliest comment date
    comment_date_divs = tree.xpath("//div[@class='comment' and not(@class='reply')]//div[@class='date']")
    if comment_date_divs:
        earliest_comment_date_div = comment_date_divs[-1]
        date_str = parse_date_string(earliest_comment_date_div.text_content().strip())
        if date_str:
            log.debug(f"Found comment date: {date_str}")
            earliest_comment_date = date_str
    
    if earliest_comment_date:
        if not release_date:
            log.info(f"No release date found, using comment date as estimated release date: {earliest_comment_date}")
            release_date = earliest_comment_date
            is_estimate = True
        else:
            try:
                comment_dt = datetime.strptime(earliest_comment_date, "%Y-%m-%d")
                release_dt = datetime.strptime(release_date, "%Y-%m-%d")
                if comment_dt < release_dt - timedelta(days=4):
                    log.info(f"Earliest comment date is significantly earlier than nominal release date. Using comment date as estimated release date: {earliest_comment_date}")
                    release_date = earliest_comment_date
                    is_estimate = True
            except ValueError:
                pass
    
    return (release_date, is_estimate)


def get_details(tree) -> str:
    xpath_desc = (
        "(//section[@id='trailer-data' or @id='sceneInfo' or @id='scene-info' or @id='des-scene'])"
        "//p[@class='description']"
    )
    paragraphs = tree.xpath(xpath_desc)
    if not paragraphs:
        xpath_all_p = (
            "(//section[@id='trailer-data' or @id='sceneInfo' or @id='scene-info' or @id='des-scene'])"
            "//p"
        )
        paragraphs = tree.xpath(xpath_all_p)
    if not paragraphs:
        paragraphs = tree.xpath("//div[@class='objectInfo']/div[@class='content']/p")
        
    if paragraphs:
        details_parts = [p.text_content().strip() for p in paragraphs if p.text_content().strip()]
        filtered_parts = []
        for part in details_parts:
            if part.startswith("Released:") or part.startswith("Tags:") or part.startswith("Models:") or part.startswith("Performers:"):
                continue
            if len(part) < 30 and parse_date_string(part):
                continue
            filtered_parts.append(part)
        if filtered_parts:
            return "\n\n".join(filtered_parts)
    return ""


def get_performers(tree) -> list:
    xpath_expr = (
        "(//section[@id='trailer-data' or @id='sceneInfo' or @id='scene-info' or @id='des-scene'])"
        "//a[@class='model-name']/@title | "
        "(//section[@id='trailer-data' or @id='sceneInfo' or @id='scene-info' or @id='des-scene'])"
        "//a[contains(@href,'/model')]/@title"
    )
    perf_titles = tree.xpath(xpath_expr)
    
    if not perf_titles:
        xpath_links = (
            "(//section[@id='trailer-data' or @id='sceneInfo' or @id='scene-info' or @id='des-scene'])"
            "//a[@class='model-name'] | "
            "(//section[@id='trailer-data' or @id='sceneInfo' or @id='scene-info' or @id='des-scene'])"
            "//a[contains(@href,'/model')]"
        )
        links = tree.xpath(xpath_links)
        perf_titles = [link.text_content().strip() for link in links if link.text_content().strip()]
        
    performers = []
    seen = set()
    for p in perf_titles:
        name = p.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            performers.append({"name": name})
    return performers


def get_tags(tree) -> list:
    xpath_expr = (
        "(//section[@id='trailer-data' or @id='sceneInfo' or @id='scene-info' or @id='des-scene'])"
        "//a[contains(@href,'/categories')] | "
        "(//section[@id='trailer-data' or @id='sceneInfo' or @id='scene-info' or @id='des-scene'])"
        "//a[contains(@href,'/category')] | "
        "//div[contains(@class, 'categories-holder')]/a"
    )
    tag_links = tree.xpath(xpath_expr)
    
    if not tag_links:
        tag_links = tree.xpath("//div[@class='objectInfo']//p[contains(text(),'Tags')]//a")
        
    tags = []
    seen = set()
    for link in tag_links:
        name = link.text_content().strip()
        if not name and hasattr(link, 'attrib') and 'title' in link.attrib:
            name = link.attrib['title'].strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            tags.append({"name": name})
    return tags


def get_image(tree, base_url: str = "") -> str:
    xpath_expr = (
        "(//section[@id='trailer-video' or @id='scene' or @id='scene-video'] | //div[contains(@class, 'videoHolder')])"
        "//video/@poster | "
        "//img[contains(@class, 'update_thumb') or contains(@class, 'trailer-thumb')]/@src"
    )
    images = tree.xpath(xpath_expr)
    
    if not images:
        scripts = tree.xpath("//script[contains(text(), 'useimage')]/text()")
        for script_content in scripts:
            useimage_match = re.search(r'useimage\s*=\s*"(.*?)"', script_content)
            if useimage_match:
                raw_url = useimage_match.group(1).strip()
                if raw_url:
                    relative_url = raw_url.replace("/members/", "/")
                    images = [relative_url]
                    break

    if images:
        img_url = images[0].strip()
        img_url = re.sub(r"[?&]img(?:q|w|h)=[^&]+", "", img_url)
        
        base_hrefs = tree.xpath("//base/@href")
        if base_hrefs:
            base_url = base_hrefs[0].strip()
        joined_url = urljoin(base_url, img_url)
        log.debug(f"Final image URL: {joined_url}")
        return joined_url
    return None


def get_studio(tree, url: str) -> dict:
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


def get_download_filename_stem(tree) -> str:
    download_links = tree.xpath(
        "//a[contains(@title, 'select save as to download')]/@href | "
        "//a[contains(@href, '/download/')]/@href | "
        "//a[contains(@class, 'download')]/@href"
    )
    if not download_links:
        return ""
    filename_stems = []
    for link in download_links:
        path = urlparse(link).path
        filename = basename(path)
        if filename:
            filename_stems.append(filename)
            
    if not filename_stems:
        return ""
    common_prefix = commonprefix(filename_stems)
    if not common_prefix:
        common_prefix = filename_stems[0]
        
    if common_prefix.endswith(".mp4"):
        common_prefix = common_prefix[:-4]
    if common_prefix.endswith("_"):
        common_prefix = common_prefix[:-1]
    
    common_prefix = re.sub(r'_(?:hd|sd|mobile|1080p|720p|4k|480p)$', '', common_prefix, flags=re.IGNORECASE)
    log.debug(f"Extracted filename stem: {common_prefix}")
    return common_prefix


def scrape_scene_data(url: str) -> dict:
    log.debug(f"Scraping URL: {url}")
    
    domain = urlparse(url).netloc.lower()
    cookies = get_pcar_cookies_dict(domain)

    if not cookies:
        presumed_public_url = get_presumed_public_url(url)
        if check_public_url_validity(presumed_public_url):
            log.warning(f"No cookie found for URL {url} . Returning public URL for the user scrape: {presumed_public_url}")
            return {"urls": [presumed_public_url]}
        else:
            log.warning(f"No cookie found for URL {url}. Unable to find working public URL. Returning Members Only tag.")
            wayback_url = f"https://web.archive.org/web/*/{presumed_public_url}"
            log.debug(f"Scene would hypothetically have public URL of {presumed_public_url}, but it is not valid. You may try checking the Wayback Machine at {wayback_url}.")
            return {"tags": [{"name": "Members Only"}]}
    
    cookies["warn"] = "true"
    log.debug(f"Using cookie authentication (sending {len(cookies) - 1} candidates)")

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

    presumed_public_url = get_presumed_public_url(scene_url)
    public_url_is_valid = check_public_url_validity(presumed_public_url)

    is_vip_scene = False
    title_text = get_title_text(tree)
    if title_text:
        scene["title"] = title_text
        if re.search(r"\bVIP\d?\b", title_text):
            is_vip_scene = True

    date_str, release_date_is_estimate = get_release_date(tree)
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
    if release_date_is_estimate:
        scene["tags"].append({"name": "Estimated Date"})

    image_url = get_image(tree, url)
    if image_url:
        scene["image"] = image_url

    scene["studio"] = get_studio(tree, url)

    scene["urls"] = [scene_url]
    if public_url_is_valid:
        scene["urls"].append(presumed_public_url)
    else:
        wayback_url = f"https://web.archive.org/web/*/{presumed_public_url}"
        log.debug(f"Scene would hypothetically have public URL of {presumed_public_url}, but it is not valid. You may try checking the Wayback Machine at {wayback_url}.")

    filename_stem = get_download_filename_stem(tree)
    if filename_stem:
        scene["code"] = filename_stem

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
