import base64
import json
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

from py_common.deps import ensure_requirements
ensure_requirements("lxml")

import py_common.log as log
from lxml import html
from py_common.proxy import StashRequests
from py_common.util import scraper_args
from py_common.config import get_config

config = get_config(
    # NOTE: Need to use `<equal_sign>` rather than literal `=` in the example in the comment
    # because config parser prioritizes looking for `=` over looking for `#`
    default="""
    # Set your member auth cookies (pcar...) here for each site to scrape member content
    # Can be the full cookie (name<equal_sign>value) or just the cookie value itself.
    # e.g. can set to pcar%5fR2xvcnlIb2xlU3dhbGx3bw%3d%3d<equal_sign>ZlN4bTh1OWFzZGRmYTg...
    # or just ZlN4bTh1OWFzZGRmYTg...
    GLORYHOLESWALLOW_PCAR =
    CUMPSTERS_PCAR =
    SPYTUG_PCAR =
    CUMCLINIC_PCAR =
"""
)

requests = StashRequests()


def get_pcar_cookie(domain: str) -> str:
    val = ""
    if "gloryholeswallow" in domain:
        val = config["GLORYHOLESWALLOW_PCAR"]
    elif "cumpsters" in domain:
        val = config["CUMPSTERS_PCAR"]
    elif "spytug" in domain:
        val = config["SPYTUG_PCAR"]
    elif "cumclinic" in domain:
        val = config["CUMCLINIC_PCAR"]
    
    if val:
        val = str(val).strip().strip("\"'")
        if "=" in val:
            parts = val.split("=", 1)
            if parts[0].strip().lower().startswith("pcar"):
                val = parts[1].strip().strip("\"'")
    return val


def get_cookie_name(domain: str) -> str:
    if "gloryholeswallow" in domain:
        name = "GloryHoleSwallwo"
    elif "cumpsters" in domain:
        name = "Cumpsters"
    elif "spytug" in domain:
        name = "SpyTug"
    elif "cumclinic" in domain:
        name = "CumClinic"
    else:
        return ""
    
    b64_name = base64.b64encode(name.encode('utf-8')).decode('utf-8')
    return f"pcar%5f{b64_name.replace('=', '%3d')}"


def rewrite_members_url(url: str) -> str:
    """Rewrite members scenes URL to public tour trailer URL if matched, unless we have cookies."""
    # TODO: Check whether a public URL is available and add it to output if so. If not, include tag `Members Only`
    # TODO: Use stem of download filenames as studio code

    # regex: \/members\/scenes\/(.*)_vids\.html -> /tour/trailers/$1.html
    match = re.search(r'/members/scenes/(.*)_vids\.html', url, re.IGNORECASE)
    if match:
        domain = urlparse(url).netloc.lower()
        cookie_val = get_pcar_cookie(domain)
        if cookie_val:
            log.debug("Found member cookie for domain. Skipping rewrite.")
            return url
        # No relevant cookie, do rewrite
        scene_id = match.group(1)
        url = re.sub(r'/members/scenes/.*_vids\.html', f'/tour/trailers/{scene_id}.html', url, flags=re.IGNORECASE)
        log.debug(f"Rewrote members URL to: {url}")
    return url


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


def get_release_date(tree, title_text: str) -> str:
    if not title_text:
        return ""
    m1 = re.search(r'([A-Za-z]+)\.?\s*(\d{1,2}),\s*(\d{4})', title_text)
    if m1:
        month_str, day_str, year_str = m1.groups()
        month_str = month_str[:3].title()
        try:
            dt = datetime.strptime(f"{month_str} {day_str} {year_str}", "%b %d %Y")
            date_str = dt.strftime("%Y-%m-%d")
            log.debug(f"Extracted date: {date_str}")
            return date_str
        except ValueError:
            pass
    return ""


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
    if not base_url:
        canonical_links = tree.xpath("//link[@rel='canonical']/@href")
        if canonical_links:
            base_url = canonical_links[0].strip()
            
    base_hrefs = tree.xpath("//base/@href")
    if base_hrefs:
        base_url = base_hrefs[0].strip()
        
    img_srcs = tree.xpath("//div[@id='fakeplayer']//img/@src0_1x") or tree.xpath("//div[@id='fakeplayer']//img/@src")
    if img_srcs:
        img_src = img_srcs[0].strip()
        if base_url:
            return urljoin(base_url, img_src)
        return img_src
    return ""


def scrape_scene_data(url: str) -> dict:
    url = rewrite_members_url(url)
    
    log.debug(f"Fetching scene URL: {url}")
    
    domain = urlparse(url).netloc.lower()
    # TODO: Only look up and use cookies if this is a members' URL
    cookie_val = get_pcar_cookie(domain)
    
    cookies = {}
    log.debug(f"cookie_val: {cookie_val}")
    if cookie_val:
        cookie_name = get_cookie_name(domain)
        log.debug(f"cookie_name: {cookie_name}")
        if cookie_name:
            cookies[cookie_name] = cookie_val
            cookies["warn"] = "true"
            redacted = cookie_val[:2] + "..." + cookie_val[-2:] if len(cookie_val) >= 5 else "REDACTED"
            log.debug(f"Using cookie authentication: '{cookie_name}' = '{redacted}'")
            
    try:
        response = requests.get(url, cookies=cookies, timeout=10)
        response.raise_for_status()
    except Exception as e:
        log.error(f"Failed to fetch URL {url}: {e}")
        return {}

    tree = html.fromstring(response.content)
    scene = {}

    # Extract Title and Date
    title_text = get_title_text(tree)
    if title_text:
        scene["title"] = title_text

    date_str = get_release_date(tree, title_text)
    if date_str:
        scene["date"] = date_str

    # Extract Details
    details = get_details(tree)
    if details:
        scene["details"] = details

    # Extract Tags
    tags = get_tags(tree)
    if tags:
        scene["tags"] = tags

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

    # Extract URL
    # XPath: //link[@rel='canonical']/@href
    canonical_links = tree.xpath("//link[@rel='canonical']/@href")
    scene_url = canonical_links[0].strip() if canonical_links else url
    scene["url"] = scene_url
    scene["urls"] = [scene_url]

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
