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

requests = StashRequests()


def rewrite_members_url(url: str) -> str:
    """Rewrite members scenes URL to public tour trailer URL if matched."""
    # TODO: Use .ini file to get members' logged-in cookies and scrape the members page, and potentially rewrite to add public URL to scrape (after checking that it doesn't 404).
    # regex: \/members\/scenes\/(.*)_vids\.html -> /tour/trailers/$1.html
    match = re.search(r'/members/scenes/(.*)_vids\.html', url, re.IGNORECASE)
    if match:
        scene_id = match.group(1)
        url = re.sub(r'/members/scenes/.*_vids\.html', f'/tour/trailers/{scene_id}.html', url, flags=re.IGNORECASE)
        log.debug(f"Rewrote members URL to: {url}")
    return url

def scrape_scene_data(url: str) -> dict:
    url = rewrite_members_url(url)
    
    log.debug(f"Fetching scene URL: {url}")
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
    except Exception as e:
        log.error(f"Failed to fetch URL {url}: {e}")
        return {}

    tree = html.fromstring(response.content)
    scene = {}

    # Extract Title and Date
    # XPath: //meta[@property='og:title']/@content
    meta_titles = tree.xpath("//meta[@property='og:title']/@content")
    title_text = ""
    if meta_titles:
        title_text = meta_titles[0].strip()
        scene["title"] = title_text

    # Extract Date from Title (where the date is displayed inside the title string)
    if title_text:
        # Try to parse current date format, e.g. Jun. 12, 2024 or June 12, 2024
        m1 = re.search(r'([A-Za-z]+)\.?\s*(\d{1,2}),\s*(\d{4})', title_text)
        if m1:
            month_str, day_str, year_str = m1.groups()
            month_str = month_str[:3].title()
            try:
                dt = datetime.strptime(f"{month_str} {day_str} {year_str}", "%b %d %Y")
                date_str = dt.strftime("%Y-%m-%d")
                scene["date"] = date_str
                log.debug(f"Extracted date: {date_str}")
            except ValueError:
                pass

    # Extract Details
    # XPath: //div[@class='objectInfo']/div[@class='content']/p/text() joined by "\n\n"
    paragraphs = tree.xpath("//div[@class='objectInfo']/div[@class='content']/p")
    if paragraphs:
        details_parts = [p.text_content().strip() for p in paragraphs if p.text_content().strip()]
        if details_parts:
            scene["details"] = "\n\n".join(details_parts)

    # Extract Tags
    # XPath: //div[@class='objectInfo']//p[contains(text(),'Tags')]//a/text()
    tag_links = tree.xpath("//div[@class='objectInfo']//p[contains(text(),'Tags')]//a")
    if tag_links:
        tags = []
        for link in tag_links:
            name = link.text_content().strip()
            if name:
                tags.append({"name": name})
        if tags:
            scene["tags"] = tags

    # Extract Image URL
    # XPath: //base/@href | //div[@id='fakeplayer']//img/@src | //div[@id='fakeplayer']//img/@src0_1x
    base_hrefs = tree.xpath("//base/@href")
    base_url = base_hrefs[0].strip() if base_hrefs else url
    
    img_srcs = tree.xpath("//div[@id='fakeplayer']//img/@src0_1x") or tree.xpath("//div[@id='fakeplayer']//img/@src")
    if img_srcs:
        img_src = img_srcs[0].strip()
        # Resolve relative/absolute URLs safely
        resolved_img_url = urljoin(base_url, img_src)
        scene["image"] = resolved_img_url
        log.debug(f"Extracted image URL: {resolved_img_url}")

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
