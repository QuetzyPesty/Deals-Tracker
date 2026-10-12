"""
LiveLaw "Deals" scraper (law-firm deal announcements only).

Discovery: /law-firms/deals listing pages. Pagination is a path suffix
(/law-firms/deals/2). robots.txt disallows `?page=`/`?sort=` query forms, which
are never used, and search/preview/embed paths, which are never visited.
Litigation stories live under /law-firms/litigation and are out of scope.

LiveLaw shows a subscription banner but serves the article text in full; if
that ever changes the paragraphs come back empty and the record simply has no
people, it is not fabricated.

Run:  python3 scraper/scrape_livelaw.py [--pages N] [--max-new N] [--reparse-all]
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import news_source  # noqa: E402


class LiveLaw(news_source.Source):
    slug = "livelaw"
    label = "LiveLaw"
    host = "livelaw.in"
    article_path = re.compile(r"^/law-firms/deals/[^/]+-\d+$")
    containers = ("article", "section.article-body-content", "div.details-story-wrapper")

    def listing_urls(self, pages):
        root = f"https://www.{self.host}/law-firms/deals"
        return [root] + [f"{root}/{n}" for n in range(2, pages + 1)]


SOURCE = LiveLaw()

if __name__ == "__main__":
    sys.exit(news_source.run(SOURCE))
