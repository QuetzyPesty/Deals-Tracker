"""
Legal Era "Deal Street" scraper.

Discovery: the three Google-News style sitemaps (each spans roughly a day) plus
the first listing pages. Pagination is a path suffix (/deal-street/2); the
`?page=` query form is disallowed by robots.txt and is never used.

Run:  python3 scraper/scrape_legalera.py [--pages N] [--max-new N] [--reparse-all]
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import news_source  # noqa: E402


class LegalEra(news_source.Source):
    slug = "legalera"
    label = "Legal Era"
    host = "legaleraonline.com"
    article_path = re.compile(r"^/deal-street/[^/]+-\d+$")
    containers = ("div.story_content", "div.details-content-story", "article")

    def sitemap_urls(self):
        root = f"https://www.{self.host}"
        return [
            f"{root}/news-sitemap-daily.xml",
            f"{root}/sitemap-daily.xml",
            f"{root}/sitemap-daily-updated.xml",
        ]

    def listing_urls(self, pages):
        root = f"https://www.{self.host}/deal-street"
        return [root] + [f"{root}/{n}" for n in range(2, pages + 1)]


SOURCE = LegalEra()

if __name__ == "__main__":
    sys.exit(news_source.run(SOURCE))
