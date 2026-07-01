import base64
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

# TODO: Support additional related sites (cumpsters, spytug, cumclinic). Currently there
# is some immediately useless parameterization in preparation for such support.

# Requires logged-in member cookie to get actual metadata. If no cookie, scraper will just
# return the logged-out URL if any exists, which can then be scraped by the non-members scraper.

config = get_config(
    # "PCAR" value can be set to 
    # NOTE: Need to use `<equal_sign>` rather than literal `=` in the example in the comment
    # because config parser prioritizes looking for `=` over looking for `#`
    default="""
    # Set your member auth cookies (pcar...) here for each site to scrape member content
    # Can be the full cookie (name<equal_sign>value) or just the cookie value itself.
    # e.g. can set to pcar%5fR2xvcnlIb2xlU3dhbGx3bw%3d%3d<equal_sign>ZlN4bTh1OWFzZGRmYTg...
    # or just ZlN4bTh1OWFzZGRmYTg...
    GLORYHOLESWALLOW_PCAR =
"""
)

requests = StashRequests()


def get_pcar_cookie(domain: str) -> str:
    val = ""
    if "gloryholeswallow" in domain:
        val = config["GLORYHOLESWALLOW_PCAR"]
    # TODO: Other sister sites
    
    if val:
        val = str(val).strip().strip("\"'")
        if "=" in val:
            parts = val.split("=", 1)
            if parts[0].strip().lower().startswith("pcar"):
                val = parts[1].strip().strip("\"'")
    return val

def get_cookie_name(domain: str) -> str:
    if "gloryholeswallow" in domain:
        name = "GloryHoleSwallwo" # [sic]
    else:
        return ""
    
    b64_name = base64.b64encode(name.encode('utf-8')).decode('utf-8')
    return f"pcar%5f{b64_name.replace('=', '%3d')}"

def has_valid_cookie(url: str) -> bool:
    domain = urlparse(url).netloc.lower()
    cookie_val = get_pcar_cookie(domain)
    if cookie_val:
        return True
    return False

def get_presumed_public_url(url: str) -> str:
    match = re.search(r'/members/scenes/(.*)_vids\.html', url, re.IGNORECASE)
    if match:
        scene_id = match.group(1)
        url = re.sub(r'/members/scenes/.*_vids\.html', f'/tour/trailers/{scene_id}.html', url, flags=re.IGNORECASE)
    return url

def get_url_to_scrape(url: str) -> str:
    """Rewrite members scenes URL to public tour trailer URL if matched, unless we have cookies."""
    presumed_public_url = get_presumed_public_url(url)
    if presumed_public_url == url:
        return url
    if has_valid_cookie(url):
        log.debug("Found member cookie for domain. Skipping rewrite.")
        return url
    else:
        return presumed_public_url


def get_title_text(tree) -> str:
    meta_titles = tree.xpath("//meta[@property='og:title']/@content")
    title_text = ""
    if meta_titles:
        title_text = meta_titles[0].strip()

    if not title_text:
        fallback_titles = tree.xpath("//div[@class='objectInfo']/h1")
        if fallback_titles:
            title_text = fallback_titles[0].text_content().strip()
    return title_text


def parse_date_string(date_text: str) -> str:
    if not date_text:
        return ""
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


def get_release_date(tree, title_text: str) -> tuple[str, bool]:
    """
    Tries to extract the release date from the page or title text. If the page includes
    a user comment with a date significantly earlier than the extracted scene release date,
    uses the comment date and returns the second element of the tuple as True.

    Returns:
        tuple[str, bool]: First element is extracted date as it appears in the input,
        or empty string if none found.
        Second element is True iff the release date is only an estimate as per the comment date logic.
    """
    release_date = ""
    earliest_comment_date = ""
    is_estimate = False
    # 0. Try extracting earliest comment date, to use in part 3.
    comment_date_divs = tree.xpath("//div[@class='comment' and not(@class='reply')]//div[@class='date']")
    if comment_date_divs:
        earliest_comment_date_div = comment_date_divs[-1]
        date_str = parse_date_string(earliest_comment_date_div.text_content().strip())
        if date_str:
            log.debug(f"Found comment date: {date_str}")
            earliest_comment_date = date_str



    # 1. Try extracting explicit release date (i.e. on members pages)
    released_spans = tree.xpath("//div[@class='objectInfo']//p[contains(., 'Released')]/span")
    if released_spans:
        date_str = parse_date_string(released_spans[0].text_content().strip())
        if date_str:
            log.debug(f"Found explicit release date: {date_str}")
            release_date = date_str

    # 2. Fallback to extracting from the title (i.e. logged-out pages)
    if not release_date and title_text:
        date_str = parse_date_string(title_text)
        if date_str:
            log.debug(f"Extracted release date from title: {date_str}")
            release_date = date_str
    
    # 3. Some scenes were reorganized and given new release dates but the comments still have their original date
    if earliest_comment_date:
        if not release_date:
            log.debug(f"No release date found, using comment date as estimated release date: {earliest_comment_date}")
            release_date = earliest_comment_date
            is_estimate = True
        else:
            try:
                comment_dt = datetime.strptime(earliest_comment_date, "%Y-%m-%d")
                release_dt = datetime.strptime(release_date, "%Y-%m-%d")
                # Leave 4 day wiggle room for release schedule weirdness
                if comment_dt < release_dt - timedelta(days=4):
                    log.debug(f"Earliest comment date is significantly earlier than nominal release date. Using comment date as estimated release date: {earliest_comment_date}")
                    release_date = earliest_comment_date
                    is_estimate = True
            except ValueError:
                pass
    
    return (release_date, is_estimate)


def get_details(tree) -> str:
    paragraphs = tree.xpath("//div[@class='objectInfo']/div[@class='content']/p")
    if paragraphs:
        details_parts = [p.text_content().strip() for p in paragraphs if p.text_content().strip()]
        if details_parts:
            return "\n\n".join(details_parts)
    return ""


def get_tags(tree) -> list:
    tag_links = tree.xpath("//div[@class='objectInfo']//p[contains(text(),'Tags')]//a")
    tags = []
    if tag_links:
        for link in tag_links:
            name = link.text_content().strip()
            if name:
                tags.append({"name": name})
    return tags


def get_image(tree, base_url: str = "") -> str:
    relative_image_url = ""

    # When logged-in, cover image URL is only avaialable as part of an inline script, as `useimage = "/members/content//contentthumbs/xyz/abc.jpg";`
    scripts = tree.xpath("//script[contains(text(), 'useimage')]/text()")
    for script_content in scripts:
        useimage_match = re.search(r'useimage\s*=\s*"(.*?)"', script_content)
        if useimage_match:
            # Image URLs that include `/members/` require a cookie to load, and we're just sending back a URL where the client might try to
            # load without a cookie, so let's map to a public URL that seems to always exist
            raw_url = useimage_match.group(1).strip()
            if raw_url:
                if "gloryholeswallow" in base_url or "cumclinic" in base_url:
                    relative_image_url = raw_url.replace("/members/", "/tour/")
                else:
                    relative_image_url = raw_url.replace("/members/", "/")
                log.info(f"Found relative image URL from useimage: {relative_image_url}")
                break

    # When logged-out, image is available within the `fakeplayer`. Newer scenes use `src0_1x` and older scenes use `src`.
    if not relative_image_url:
        img_srcs = tree.xpath("//div[@id='fakeplayer']//img/@src0_1x") or tree.xpath("//div[@id='fakeplayer']//img/@src")
        if img_srcs:
            relative_image_url = img_srcs[0].strip()
            log.info(f"Found relative image URL from fakeplayer: {relative_image_url}")

    if relative_image_url:
        base_hrefs = tree.xpath("//base/@href")
        if base_hrefs:
            base_url = base_hrefs[0].strip()
        return urljoin(base_url, relative_image_url)
    return None


def check_public_url_validity(public_url: str) -> bool:
    """
    Checks if the public URL returns a 200 status code.
    """
    log.debug(f"Checking if public URL is valid: {public_url}")
    try:
        # Check the public URL by making a HEAD request. We disallow redirects
        # because a hypothetical public URL for a scene which is not publicly visible redirects (302) to the tour index page.
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

def get_download_filename_stem(tree):
    # Only works on members' pages
    # Get all links whose title attribute includes 'select save as to download'
    download_links = tree.xpath("//a[contains(@title, 'select save as to download')]/@href")
    if not download_links:
        return ""
    # Get the base file name from all download links
    filename_stems = []
    for link in download_links:
        filename_stems.append(basename(urlparse(link).path))
    if not filename_stems:
        return ""
    # Take the longest common prefix
    common_prefix = commonprefix(filename_stems)
    if common_prefix.endswith(".mp4"):
        common_prefix = common_prefix[:-4]
    if common_prefix.endswith("_"):
        common_prefix = common_prefix[:-1]
    return common_prefix

def scrape_scene_data(url: str) -> dict:
    url = get_url_to_scrape(url)
    
    log.debug(f"Fetching scene URL: {url}")
    
    domain = urlparse(url).netloc.lower()
    # TODO: Only look up and use cookies if this is a members' URL
    cookie_val = get_pcar_cookie(domain)
    
    cookies = {}
    if cookie_val:
        cookie_name = get_cookie_name(domain)
        log.debug(f"cookie_name: {cookie_name}")
        if cookie_name:
            cookies[cookie_name] = cookie_val
            cookies["warn"] = "true"
            redacted = cookie_val[:2] + "..." + cookie_val[-2:] if len(cookie_val) >= 12 else "REDACTED"
            log.debug(f"Using cookie authentication: '{cookie_name}' = '{redacted}'")
            
    try:
        response = requests.get(url, cookies=cookies, timeout=10)
        response.raise_for_status()
    except Exception as e:
        log.error(f"Failed to fetch URL {url}: {e}")
        return {}

    tree = html.fromstring(response.content)
    scene = {}

    # Extract URL early to check for public URL
    canonical_links = tree.xpath("//link[@rel='canonical']/@href")
    scene_url = canonical_links[0].strip() if canonical_links else url

    # Determine and check presumed public URL
    presumed_public_url = get_presumed_public_url(scene_url)
    has_presumed_public = (scene_url != presumed_public_url)
    public_url_is_valid = False

    if has_presumed_public:
        public_url_is_valid = check_public_url_validity(presumed_public_url)

    # Extract Title and Date
    is_vip_scene = False
    title_text = get_title_text(tree)
    if title_text:
        scene["title"] = title_text
        if re.search(r"\bVIP\d?\b", title_text): # e.g. `Title (VIP1)` or `Title (VIP 2)` or `Title (VIP)`
            is_vip_scene = True

    date_str, release_date_is_estimate = get_release_date(tree, title_text)
    if date_str:
        scene["date"] = date_str

    # Extract Details
    details = get_details(tree)
    if details:
        scene["details"] = details

    # Extract Tags
    scene["tags"] = get_tags(tree)
    if is_vip_scene:
        scene["tags"].append({"name": "Bonus Scenes"})
    if has_presumed_public and not public_url_is_valid:
        scene["tags"].append({"name": "Members Only"})
    if release_date_is_estimate:
        scene["tags"].append({"name": "Estimated Date"})
    

    # Extract Image URL
    image_url = get_image(tree, url)
    if image_url:
        scene["image"] = image_url
        log.debug(f"Extracted image URL: {image_url}")

    # Extract Studio
    # Map domain names to Studio names
    domain = urlparse(url).netloc.lower()
    studio_name = None
    if "cumpsters" in domain:
        studio_name = "Cumpsters"
    elif "gloryholeswallow" in domain:
        studio_name = "Gloryhole Swallow"
    elif "spytug" in domain:
        studio_name = "SpyTug"
    elif "cumclinic" in domain:
        studio_name = "CumClinic"

    if studio_name:
        scene["studio"] = {"name": studio_name}
        log.debug(f"Mapped studio: {studio_name}")

    scene["url"] = scene_url
    scene["urls"] = [scene_url]
    if has_presumed_public:
        if public_url_is_valid:
            scene["urls"].append(presumed_public_url)
        else:
            wayback_url = f"https://web.archive.org/web/*/{presumed_public_url}"
            log.debug(f"Scene would hypothetically have public URL of {presumed_public_url}, but it is not valid. You may try checking the Wayback Machine at {wayback_url}.")

    filename_stem = get_download_filename_stem(tree)
    if filename_stem:
        scene["code"] = filename_stem
        log.debug(f"Extracted filename stem: {filename_stem}")

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
        # Print empty JSON on failure/no match
        print(json.dumps({}))
