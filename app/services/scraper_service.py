import json
import re
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional
from urllib.parse import urljoin

import anthropic
from bs4 import BeautifulSoup

from app.services.html_fetcher_service import HtmlFetcherService

logger = logging.getLogger(__name__)

# Prompt for generating CSS selectors
SELECTOR_GENERATION_PROMPT = """Analyze this HTML from a shopping website and extract CSS selectors for product listings.

The page URL is: {url}

I need you to identify CSS selectors that can extract product information from this page.
Look for product cards, listings, or grid items that contain products.

Return ONLY a valid JSON object with this exact structure (no markdown, no explanation):
{{
    "product_container": "CSS selector for individual product cards/items",
    "name": "CSS selector for product name (relative to container)",
    "price": "CSS selector for current price (relative to container)",
    "original_price": "CSS selector for original/crossed-out price if exists (relative to container), or null",
    "image": "CSS selector for product image (relative to container)",
    "link": "CSS selector for product link (relative to container)",
    "description": "CSS selector for short description if exists (relative to container), or null",
    "requires_javascript": false,
    "notes": "Any notes about the page structure"
}}

Important rules:
1. The product_container should match multiple product items on the page
2. All other selectors are RELATIVE to the product_container
3. For price selectors, target the element containing the price number
4. For image, target the img element or element with background-image
5. For link, target the anchor element with href
6. If a field doesn't exist, use null
7. Set requires_javascript to true if you detect signs the page needs JS to render products

Common patterns to look for:
- data-sku, data-product-id, data-pid attributes on product containers
- aria-label attributes often contain product name and price
- Classes like "product", "card", "item", "tile"
- Price in elements with classes like "price", "sale-price", "final-price"
- Images may use data-src or srcset for lazy loading

HTML content:
{html}"""

# Prompt for extracting products directly (fallback)
DIRECT_EXTRACTION_PROMPT = """Analyze this HTML from a shopping website and extract all products you can find.

The page URL is: {url}

Extract all products visible on this page. For each product, extract:
- name: Product title/name
- price: Current selling price (number only, no currency symbol). Convert from local format (e.g., €590 -> 590)
- original_price: Original price if on sale (number only), or null
- image_url: URL of the product image (look for img src, data-src, or srcset)
- product_url: URL to the product detail page (full URL, not relative)
- description: Short description or color/variant info if available, or null

Tips for finding products:
- Look for repeated structures with similar classes (cards, tiles, items)
- Check aria-label attributes - they often contain product name and price
- Look for data-sku, data-product-id attributes
- Prices may be in elements with classes containing "price", "cost", "sale"
- Product names are often in h2, h3, or elements with "title" in the class

Return ONLY a valid JSON array with this structure (no markdown, no explanation):
[
    {{
        "name": "Product Name",
        "price": 29.99,
        "original_price": 49.99,
        "image_url": "https://...",
        "product_url": "https://...",
        "description": "Short description"
    }}
]

If no products found, return an empty array: []

HTML content:
{html}"""


class ScraperService:
    """Service for scraping products from shopping websites using LLM-generated selectors"""

    def __init__(self, api_key: str, model: str = 'claude-3-5-haiku-latest'):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.fetcher = HtmlFetcherService()
        self.tokens_used = 0

    def analyze_url(self, url: str) -> tuple[Optional[dict], Optional[str]]:
        """
        Analyze a URL and generate CSS selectors for product extraction.
        Returns (selectors_dict, error_message)
        """
        # Fetch the page
        html, error = self.fetcher.fetch(url)
        if error:
            return None, error

        # Clean HTML for LLM
        cleaned_html = self.fetcher.clean_html_for_llm(html)

        # Generate selectors using LLM
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2000,
                messages=[{
                    'role': 'user',
                    'content': SELECTOR_GENERATION_PROMPT.format(url=url, html=cleaned_html)
                }]
            )

            self.tokens_used += response.usage.input_tokens + response.usage.output_tokens

            # Parse the JSON response
            response_text = response.content[0].text.strip()

            # Try to extract JSON from response
            selectors = self._parse_json_response(response_text)

            if selectors:
                # Validate the selectors work
                test_products = self._extract_with_selectors(html, selectors, url)
                if test_products:
                    selectors['_sample_count'] = len(test_products)
                    return selectors, None
                else:
                    # Selectors didn't work, try direct extraction
                    logger.warning("Generated selectors didn't extract any products, falling back to direct extraction")

            # Fallback: Direct LLM extraction
            return self._generate_selectors_from_direct_extraction(html, url)

        except anthropic.APIError as e:
            return None, f"API error: {str(e)}"
        except Exception as e:
            logger.exception("Error analyzing URL")
            return None, f"Error analyzing page: {str(e)}"

    def analyze_url_with_html(self, url: str, cleaned_html: str) -> tuple[Optional[dict], Optional[str]]:
        """
        Analyze pre-cleaned HTML and generate CSS selectors for product extraction.
        Returns (selectors_dict, error_message)
        """
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2000,
                messages=[{
                    'role': 'user',
                    'content': SELECTOR_GENERATION_PROMPT.format(url=url, html=cleaned_html)
                }]
            )

            self.tokens_used += response.usage.input_tokens + response.usage.output_tokens

            # Parse the JSON response
            response_text = response.content[0].text.strip()

            # Try to extract JSON from response
            selectors = self._parse_json_response(response_text)

            if selectors:
                return selectors, None

            # Fallback
            return self._generate_selectors_from_direct_extraction(cleaned_html, url)

        except anthropic.APIError as e:
            return None, f"API error: {str(e)}"
        except Exception as e:
            logger.exception("Error analyzing HTML")
            return None, f"Error analyzing page: {str(e)}"

    def _parse_json_response(self, text: str) -> Optional[dict]:
        """Parse JSON from LLM response, handling various formats"""
        # Try direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try to find JSON in markdown code block
        json_match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        # Try to find raw JSON object
        json_match = re.search(r'\{[\s\S]*\}', text)
        if json_match:
            try:
                return json.loads(json_match.group(0))
            except json.JSONDecodeError:
                pass

        return None

    def _extract_with_selectors(self, html: str, selectors: dict, base_url: str) -> list[dict]:
        """Extract products using CSS selectors"""
        soup = BeautifulSoup(html, 'lxml')
        products = []

        container_selector = selectors.get('product_container')
        if not container_selector:
            return []

        containers = soup.select(container_selector)

        for container in containers[:50]:  # Limit to 50 products
            try:
                product = self._extract_product_from_container(container, selectors, base_url)
                if product and product.get('name'):
                    products.append(product)
            except Exception as e:
                logger.debug(f"Error extracting from container: {e}")
                continue

        return products

    def _extract_product_from_container(self, container, selectors: dict, base_url: str) -> Optional[dict]:
        """Extract product data from a single container using selectors"""
        product = {}

        # Extract name
        name_selector = selectors.get('name')
        if name_selector:
            name_el = container.select_one(name_selector)
            if name_el:
                product['name'] = name_el.get_text(strip=True)

        # Extract price
        price_selector = selectors.get('price')
        if price_selector:
            price_el = container.select_one(price_selector)
            if price_el:
                product['price'] = self._parse_price(price_el.get_text())

        # Extract original price
        original_price_selector = selectors.get('original_price')
        if original_price_selector:
            orig_price_el = container.select_one(original_price_selector)
            if orig_price_el:
                product['original_price'] = self._parse_price(orig_price_el.get_text())

        # Extract image
        image_selector = selectors.get('image')
        if image_selector:
            img_el = container.select_one(image_selector)
            if img_el:
                img_url = img_el.get('src') or img_el.get('data-src') or img_el.get('data-lazy-src')
                if img_url:
                    product['image_url'] = self.fetcher.normalize_url(img_url, base_url)

        # Extract link
        link_selector = selectors.get('link')
        if link_selector:
            link_el = container.select_one(link_selector)
            if link_el:
                href = link_el.get('href')
                if href:
                    product['product_url'] = self.fetcher.normalize_url(href, base_url)

        # Extract description
        desc_selector = selectors.get('description')
        if desc_selector:
            desc_el = container.select_one(desc_selector)
            if desc_el:
                product['description'] = desc_el.get_text(strip=True)[:500]

        return product if product.get('name') else None

    def _parse_price(self, text: str) -> Optional[Decimal]:
        """Parse price from text, handling various formats"""
        if not text:
            return None

        # Remove currency symbols and whitespace
        cleaned = re.sub(r'[^\d.,]', '', text)

        # Handle different decimal formats
        if ',' in cleaned and '.' in cleaned:
            # Determine which is decimal separator
            if cleaned.rfind(',') > cleaned.rfind('.'):
                # European format: 1.234,56
                cleaned = cleaned.replace('.', '').replace(',', '.')
            else:
                # US format: 1,234.56
                cleaned = cleaned.replace(',', '')
        elif ',' in cleaned:
            # Could be decimal or thousands separator
            parts = cleaned.split(',')
            if len(parts) == 2 and len(parts[1]) == 2:
                # Likely decimal: 12,99
                cleaned = cleaned.replace(',', '.')
            else:
                # Likely thousands: 1,234
                cleaned = cleaned.replace(',', '')

        try:
            return Decimal(cleaned)
        except (InvalidOperation, ValueError):
            return None

    def _generate_selectors_from_direct_extraction(self, html: str, url: str) -> tuple[Optional[dict], Optional[str]]:
        """Generate pseudo-selectors based on direct LLM extraction"""
        # This creates a fallback that signals we need to use LLM for extraction
        return {
            'product_container': None,
            'use_llm_extraction': True,
            'requires_javascript': False,
            'notes': 'CSS selectors could not be determined. Will use LLM-based extraction.'
        }, None

    def extract_products(self, url: str, selectors: dict, html: str = None) -> tuple[list[dict], Optional[str], bool]:
        """
        Extract products from a URL using stored selectors.
        Returns (products_list, error_message, selectors_regenerated)
        """
        selectors_regenerated = False

        # Fetch HTML if not provided
        if not html:
            requires_js = selectors.get('requires_javascript', False)
            print(f"[SCRAPER] Fetching HTML (JS required: {requires_js})...")
            html, error = self.fetcher.fetch(url, require_javascript=requires_js)
            if error:
                print(f"[SCRAPER] Fetch failed: {error}")
                return [], error, False
            print(f"[SCRAPER] Fetched {len(html):,} bytes")

        # Check if we need LLM extraction
        if selectors.get('use_llm_extraction'):
            print("[SCRAPER] Using LLM extraction mode...")
            products, error = self._extract_with_llm(html, url)
            return products, error, False

        # Try selector-based extraction
        print("[SCRAPER] Trying selector-based extraction...")
        products = self._extract_with_selectors(html, selectors, url)

        # If no products found, regenerate selectors
        if not products:
            print("[SCRAPER] No products found, regenerating selectors...")
            logger.info(f"No products found with selectors for {url}, regenerating...")
            new_selectors, error = self.analyze_url(url)
            if error:
                return [], error, False

            if new_selectors and not new_selectors.get('use_llm_extraction'):
                products = self._extract_with_selectors(html, new_selectors, url)
                selectors_regenerated = True

            # Still no products? Try direct LLM extraction
            if not products:
                print("[SCRAPER] Falling back to direct LLM extraction...")
                products, error = self._extract_with_llm(html, url)
                return products, error, selectors_regenerated

        print(f"[SCRAPER] Extracted {len(products)} products")
        return products, None, selectors_regenerated

    def _extract_with_llm(self, html: str, url: str) -> tuple[list[dict], Optional[str]]:
        """Extract products directly using LLM (fallback method)"""
        cleaned_html = self.fetcher.clean_html_for_llm(html, max_length=40000)

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4000,
                messages=[{
                    'role': 'user',
                    'content': DIRECT_EXTRACTION_PROMPT.format(url=url, html=cleaned_html)
                }]
            )

            self.tokens_used += response.usage.input_tokens + response.usage.output_tokens

            response_text = response.content[0].text.strip()

            # Parse products JSON
            products = self._parse_products_json(response_text)

            # Normalize URLs
            for product in products:
                if product.get('image_url'):
                    product['image_url'] = self.fetcher.normalize_url(product['image_url'], url)
                if product.get('product_url'):
                    product['product_url'] = self.fetcher.normalize_url(product['product_url'], url)
                # Convert price to Decimal
                if product.get('price'):
                    product['price'] = Decimal(str(product['price']))
                if product.get('original_price'):
                    product['original_price'] = Decimal(str(product['original_price']))

            return products, None

        except Exception as e:
            logger.exception("Error in LLM extraction")
            return [], f"LLM extraction error: {str(e)}"

    def _parse_products_json(self, text: str) -> list[dict]:
        """Parse products JSON array from LLM response"""
        # Try direct parse
        try:
            result = json.loads(text)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

        # Try to find JSON array in markdown code block
        json_match = re.search(r'```(?:json)?\s*(\[[\s\S]*?\])\s*```', text)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        # Try to find raw JSON array
        json_match = re.search(r'\[[\s\S]*\]', text)
        if json_match:
            try:
                return json.loads(json_match.group(0))
            except json.JSONDecodeError:
                pass

        return []

    def preview_extraction(self, url: str) -> tuple[Optional[dict], list[dict], Optional[str]]:
        """
        Preview what would be extracted from a URL.
        Returns (selectors, sample_products, error_message)
        """
        # Analyze the URL to get selectors
        selectors, error = self.analyze_url(url)
        if error:
            return None, [], error

        # Fetch and extract
        html, fetch_error = self.fetcher.fetch(url, selectors.get('requires_javascript', False))
        if fetch_error:
            return selectors, [], fetch_error

        if selectors.get('use_llm_extraction'):
            products, extract_error = self._extract_with_llm(html, url)
        else:
            products = self._extract_with_selectors(html, selectors, url)
            extract_error = None

        return selectors, products[:10], extract_error  # Return max 10 for preview

    def get_tokens_used(self) -> int:
        """Get total tokens used in this session"""
        return self.tokens_used
