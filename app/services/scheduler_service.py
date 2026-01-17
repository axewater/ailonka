import logging
from datetime import datetime, timedelta
from decimal import Decimal

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app import db
from app.models import Source, Product, PriceHistory, SyncLog, UserSettings
from app.services.scraper_service import ScraperService

logger = logging.getLogger(__name__)

# Global scheduler instance
scheduler = None


def get_scheduler():
    """Get or create the scheduler instance"""
    global scheduler
    if scheduler is None:
        scheduler = BackgroundScheduler()
    return scheduler


def init_scheduler(app):
    """Initialize the scheduler with the Flask app context"""
    global scheduler
    scheduler = get_scheduler()

    # Add the main sync check job - runs every 15 minutes
    scheduler.add_job(
        func=lambda: check_and_sync_sources(app),
        trigger=IntervalTrigger(minutes=15),
        id='sync_check',
        name='Check for sources needing sync',
        replace_existing=True
    )

    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started")


def shutdown_scheduler():
    """Shutdown the scheduler"""
    global scheduler
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


def check_and_sync_sources(app):
    """Check for sources that need syncing and sync them"""
    with app.app_context():
        now = datetime.utcnow()

        # Find sources that need syncing
        sources = Source.query.filter(
            Source.status == 'active',
            db.or_(
                Source.next_sync_at <= now,
                Source.next_sync_at.is_(None)
            )
        ).all()

        for source in sources:
            try:
                sync_source(source)
            except Exception as e:
                logger.exception(f"Error syncing source {source.id}: {e}")


def sync_source(source: Source) -> SyncLog:
    """
    Sync a single source - fetch products and update database.
    Returns the SyncLog record.
    """
    # Create sync log
    sync_log = SyncLog(source_id=source.id)
    db.session.add(sync_log)
    db.session.commit()

    try:
        # Get user's API key
        user_settings = UserSettings.query.filter_by(user_id=source.user_id).first()
        if not user_settings or not user_settings.get_api_key():
            raise ValueError("No API key configured for user")

        api_key = user_settings.get_api_key()
        model = user_settings.selected_model or 'claude-3-5-haiku-latest'

        # Create scraper service
        scraper = ScraperService(api_key=api_key, model=model)

        # Get selectors or generate new ones
        selectors = source.selectors
        if not selectors:
            selectors, error = scraper.analyze_url(source.base_url)
            if error:
                raise ValueError(f"Failed to analyze URL: {error}")
            source.selectors = selectors
            source.selector_version = 1

        # Extract products
        products_data, error, selectors_regenerated = scraper.extract_products(
            source.base_url,
            selectors
        )

        if error:
            raise ValueError(f"Failed to extract products: {error}")

        # Update selectors if regenerated
        if selectors_regenerated:
            new_selectors, _ = scraper.analyze_url(source.base_url)
            if new_selectors:
                source.selectors = new_selectors
                source.selector_version += 1
                sync_log.selectors_regenerated = True

        # Process extracted products
        products_added = 0
        products_updated = 0

        for product_data in products_data:
            product_url = product_data.get('product_url')
            if not product_url:
                continue

            # Find existing product by URL
            existing = Product.query.filter_by(
                source_id=source.id,
                product_url=product_url
            ).first()

            if existing:
                # Update existing product
                updated = update_product(existing, product_data)
                if updated:
                    products_updated += 1
            else:
                # Create new product
                create_product(source.id, product_data)
                products_added += 1

        # Update sync log
        sync_log.status = 'success'
        sync_log.completed_at = datetime.utcnow()
        sync_log.products_found = len(products_data)
        sync_log.products_added = products_added
        sync_log.products_updated = products_updated
        sync_log.llm_tokens_used = scraper.get_tokens_used()

        # Update source
        source.last_synced_at = datetime.utcnow()
        source.next_sync_at = datetime.utcnow() + timedelta(hours=source.sync_interval_hours)
        source.consecutive_failures = 0
        source.last_error = None

        db.session.commit()
        logger.info(f"Synced source {source.id}: {products_added} added, {products_updated} updated")

        return sync_log

    except Exception as e:
        logger.exception(f"Error syncing source {source.id}")

        # Update sync log with failure
        sync_log.status = 'failed'
        sync_log.completed_at = datetime.utcnow()
        sync_log.error_message = str(e)

        # Update source with failure
        source.consecutive_failures += 1
        source.last_error = str(e)

        # Mark as error after 3 consecutive failures
        if source.consecutive_failures >= 3:
            source.status = 'error'

        # Still schedule next sync with exponential backoff
        backoff_hours = min(source.sync_interval_hours * (2 ** source.consecutive_failures), 168)  # Max 1 week
        source.next_sync_at = datetime.utcnow() + timedelta(hours=backoff_hours)

        db.session.commit()

        return sync_log


def create_product(source_id: int, data: dict) -> Product:
    """Create a new product from extracted data"""
    product = Product(
        source_id=source_id,
        name=data.get('name', 'Unknown Product')[:500],
        product_url=data.get('product_url'),
        image_url=data.get('image_url'),
        current_price=data.get('price'),
        original_price=data.get('original_price'),
        description=data.get('description'),
        is_available=True
    )
    db.session.add(product)
    db.session.flush()  # Get the product ID

    # Add initial price history
    if product.current_price:
        history = PriceHistory(
            product_id=product.id,
            price=product.current_price
        )
        db.session.add(history)

    return product


def update_product(product: Product, data: dict) -> bool:
    """Update an existing product with new data. Returns True if price changed."""
    price_changed = False
    new_price = data.get('price')

    # Check if price changed
    if new_price and product.current_price != new_price:
        # Add to price history
        history = PriceHistory(
            product_id=product.id,
            price=new_price
        )
        db.session.add(history)
        product.current_price = new_price
        price_changed = True

    # Update other fields
    if data.get('name'):
        product.name = data['name'][:500]
    if data.get('image_url'):
        product.image_url = data['image_url']
    if data.get('original_price'):
        product.original_price = data['original_price']
    if data.get('description'):
        product.description = data['description']

    product.is_available = True
    product.last_updated_at = datetime.utcnow()

    return price_changed


def mark_unavailable_products(source_id: int, found_urls: set):
    """Mark products not found in sync as unavailable"""
    products = Product.query.filter(
        Product.source_id == source_id,
        Product.is_available == True,
        ~Product.product_url.in_(found_urls)
    ).all()

    for product in products:
        product.is_available = False


def get_sync_stats(source_id: int) -> dict:
    """Get sync statistics for a source"""
    logs = SyncLog.query.filter_by(source_id=source_id).order_by(SyncLog.started_at.desc()).limit(10).all()

    if not logs:
        return {
            'total_syncs': 0,
            'success_rate': 0,
            'avg_products_found': 0
        }

    success_count = sum(1 for log in logs if log.status == 'success')
    avg_products = sum(log.products_found or 0 for log in logs) / len(logs)

    return {
        'total_syncs': len(logs),
        'success_rate': (success_count / len(logs)) * 100,
        'avg_products_found': round(avg_products, 1),
        'recent_logs': logs
    }
