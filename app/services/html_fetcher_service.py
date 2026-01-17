import random
import time
import logging
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Pool of real browser user agents for rotation
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0',
]


class HtmlFetcherService:
    """Service for fetching HTML content from shopping websites"""

    def __init__(self, use_javascript: bool = False):
        self.use_javascript = use_javascript
        self.session = requests.Session()
        self._setup_session()

    def _setup_session(self):
        """Configure session with default headers"""
        self.session.headers.update({
            'User-Agent': random.choice(USER_AGENTS),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        })

    def _random_delay(self, min_seconds: float = 1.0, max_seconds: float = 3.0):
        """Add random delay between requests to appear more human-like"""
        delay = random.uniform(min_seconds, max_seconds)
        time.sleep(delay)

    def _rotate_user_agent(self):
        """Rotate to a new user agent"""
        self.session.headers['User-Agent'] = random.choice(USER_AGENTS)

    def fetch_static(self, url: str, timeout: int = 30) -> tuple[Optional[str], Optional[str]]:
        """
        Fetch HTML using requests (static content only).
        Returns (html_content, error_message)
        """
        try:
            self._rotate_user_agent()
            response = self.session.get(url, timeout=timeout, allow_redirects=True)
            response.raise_for_status()

            # Check for CAPTCHA indicators
            if self._detect_captcha(response.text):
                return None, "CAPTCHA detected - site may be blocking automated access"

            return response.text, None

        except requests.exceptions.Timeout:
            return None, f"Request timed out after {timeout} seconds"
        except requests.exceptions.ConnectionError:
            return None, "Failed to connect to the website"
        except requests.exceptions.HTTPError as e:
            return None, f"HTTP error: {e.response.status_code}"
        except Exception as e:
            return None, f"Error fetching page: {str(e)}"

    def fetch_with_javascript(self, url: str, timeout: int = 60000) -> tuple[Optional[str], Optional[str]]:
        """
        Fetch HTML using Playwright for JavaScript-rendered content.
        Returns (html_content, error_message)
        """
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                # Launch with stealth settings
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        '--disable-blink-features=AutomationControlled',
                        '--disable-dev-shm-usage',
                        '--no-sandbox',
                    ]
                )

                # Create context with realistic browser fingerprint
                context = browser.new_context(
                    user_agent=random.choice(USER_AGENTS),
                    viewport={'width': 1920, 'height': 1080},
                    locale='en-US',
                    timezone_id='America/New_York',
                    geolocation={'latitude': 40.7128, 'longitude': -74.0060},
                    permissions=['geolocation'],
                    java_script_enabled=True,
                    bypass_csp=True,
                    extra_http_headers={
                        'Accept-Language': 'en-US,en;q=0.9',
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                    }
                )

                page = context.new_page()

                # Add stealth scripts to avoid detection
                page.add_init_script("""
                    // Overwrite the 'webdriver' property to return undefined
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    });

                    // Overwrite plugins to look more realistic
                    Object.defineProperty(navigator, 'plugins', {
                        get: () => [1, 2, 3, 4, 5]
                    });

                    // Overwrite languages
                    Object.defineProperty(navigator, 'languages', {
                        get: () => ['en-US', 'en']
                    });
                """)

                # Navigate with realistic behavior
                try:
                    page.goto(url, timeout=timeout, wait_until='domcontentloaded')
                except Exception:
                    # Try with less strict wait condition
                    page.goto(url, timeout=timeout, wait_until='commit')

                # Wait for content to load
                page.wait_for_timeout(3000)

                # Scroll down to trigger lazy loading
                page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                page.wait_for_timeout(1000)

                html = page.content()

                # Check for CAPTCHA
                if self._detect_captcha(html):
                    browser.close()
                    return None, "CAPTCHA detected - site may be blocking automated access"

                browser.close()
                return html, None

        except Exception as e:
            return None, f"Error fetching with JavaScript: {str(e)}"

    def fetch(self, url: str, require_javascript: bool = False) -> tuple[Optional[str], Optional[str]]:
        """
        Smart fetch - tries static first, falls back to JS if needed.
        Returns (html_content, error_message)
        """
        if require_javascript:
            return self.fetch_with_javascript(url)

        # Try static fetch first
        html, error = self.fetch_static(url)

        # If static fetch failed with HTTP error, timeout, connection error, or CAPTCHA, try Playwright
        if error and ('HTTP error' in error or 'CAPTCHA' in error or 'timed out' in error or 'connect' in error.lower()):
            logger.info(f"Static fetch failed ({error}), trying Playwright for {url}")
            js_html, js_error = self.fetch_with_javascript(url)
            if js_html:
                return js_html, None
            # Return original error if Playwright also failed
            return None, f"{error}. Playwright also failed: {js_error}"

        if html:
            # Check if the page seems to require JavaScript
            soup = BeautifulSoup(html, 'lxml')

            # Indicators that JS might be required
            body_text = soup.get_text(strip=True)
            js_required_indicators = [
                'enable javascript',
                'javascript is required',
                'please enable javascript',
                'loading...',
            ]

            if len(body_text) < 500 or any(ind in body_text.lower() for ind in js_required_indicators):
                logger.info(f"Static fetch may be incomplete, trying JavaScript for {url}")
                js_html, js_error = self.fetch_with_javascript(url)
                if js_html:
                    return js_html, None

        return html, error

    def _detect_captcha(self, html: str) -> bool:
        """Detect common CAPTCHA patterns in HTML"""
        captcha_indicators = [
            'captcha',
            'recaptcha',
            'hcaptcha',
            'challenge-form',
            'bot-detection',
            'verify you are human',
            'are you a robot',
            'prove you are not a robot',
        ]
        html_lower = html.lower()
        return any(indicator in html_lower for indicator in captcha_indicators)

    def extract_domain(self, url: str) -> str:
        """Extract domain from URL"""
        parsed = urlparse(url)
        return parsed.netloc

    def normalize_url(self, url: str, base_url: str) -> str:
        """Normalize relative URLs to absolute"""
        if url.startswith('//'):
            return 'https:' + url
        if url.startswith('/'):
            parsed = urlparse(base_url)
            return f"{parsed.scheme}://{parsed.netloc}{url}"
        if not url.startswith(('http://', 'https://')):
            return base_url.rstrip('/') + '/' + url
        return url

    def clean_html_for_llm(self, html: str, max_length: int = 80000) -> str:
        """
        Clean HTML for LLM analysis - remove scripts, styles, and unnecessary content.
        Keeps structure and product-related content.
        Uses progressive cleaning - more aggressive only if needed.
        """
        soup = BeautifulSoup(html, 'lxml')

        # === PASS 1: Basic cleaning ===
        # Remove script and style elements
        for element in soup(['script', 'style', 'noscript', 'iframe', 'svg']):
            element.decompose()

        # Remove head but keep body
        head = soup.find('head')
        if head:
            head.decompose()

        # Remove comments
        from bs4 import Comment
        for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract()

        # Remove hidden elements
        for element in soup.find_all(attrs={'style': lambda x: x and 'display:none' in x.replace(' ', '')}):
            element.decompose()
        for element in soup.find_all(attrs={'hidden': True}):
            element.decompose()

        # Remove common non-product sections to save space
        for selector in ['header', 'footer', 'nav', '[class*="cookie"]', '[class*="newsletter"]',
                         '[class*="popup"]', '[class*="modal"]', '[class*="banner"]', '[id*="cookie"]']:
            for element in soup.select(selector):
                element.decompose()

        # Try to find and prioritize product listing area
        product_area = None
        for selector in ['[class*="plp"]', '[class*="product-list"]', '[class*="product-grid"]',
                         '[class*="listing"]', 'main', '[role="main"]', '[class*="results"]']:
            product_area = soup.select_one(selector)
            if product_area:
                break

        # If we found a product area and it has product-like elements, use just that
        if product_area:
            product_elements = product_area.select('[data-sku], [data-product], [class*="product"], [class*="card"]')
            if len(product_elements) > 3:
                cleaned = str(product_area)
                if len(cleaned) <= max_length:
                    return cleaned

        # Get result of pass 1
        cleaned = str(soup)

        # If small enough, return
        if len(cleaned) <= max_length:
            return cleaned

        # === PASS 2: Aggressive cleaning (only if still too large) ===
        logger.info(f"HTML still too large ({len(cleaned):,} chars), applying aggressive cleaning...")

        # Find all product-like containers (including modern React/Next.js patterns)
        product_containers = soup.select(
            '[data-sku], [data-product-id], [data-pid], [data-item-id], '
            '[class*="product-card"], [class*="product-tile"], [class*="product-item"], '
            '[class*="productCard"], [class*="ProductCard"], [class*="ProductGrid_grid_item"], '
            '[class*="_product_card"], [class*="product_card"], [class*="grid_item_wrapper"], '
            '[class*="ProductTile"], [class*="product-listing"], [class*="plp-product"]'
        )

        if product_containers:
            # Take a sample of products (first 15) to keep size manageable
            sample_size = min(15, len(product_containers))
            sample_html = f"<!-- Found {len(product_containers)} products, showing {sample_size} samples -->\n"
            sample_html += "<div class='product-samples'>\n"
            for container in product_containers[:sample_size]:
                sample_html += str(container) + "\n"
            sample_html += "</div>"

            if len(sample_html) <= max_length:
                logger.info(f"Using {sample_size} product samples ({len(sample_html):,} chars)")
                return sample_html

        # === PASS 3: Even more aggressive - strip attributes ===
        if len(cleaned) > max_length:
            logger.info("Stripping non-essential attributes...")
            # Remove most attributes except essential ones
            for tag in soup.find_all(True):
                attrs_to_keep = {}
                for attr in ['class', 'id', 'href', 'src', 'data-src', 'alt', 'title',
                             'data-sku', 'data-product', 'data-price', 'aria-label']:
                    if attr in tag.attrs:
                        attrs_to_keep[attr] = tag.attrs[attr]
                tag.attrs = attrs_to_keep

            cleaned = str(soup)

        # Final truncation if still too long
        if len(cleaned) > max_length:
            cleaned = cleaned[:max_length] + "\n<!-- HTML truncated -->"

        return cleaned
