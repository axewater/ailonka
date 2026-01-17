from datetime import datetime, timedelta
from decimal import Decimal

from flask import Blueprint, render_template, redirect, url_for, request, flash, jsonify, session
from flask_login import login_user, logout_user, login_required, current_user
from app.models import User, UserSettings, Source, Product, PriceHistory, SyncLog
from app.services.anthropic_service import AnthropicService, AVAILABLE_MODELS
from app.services.scraper_service import ScraperService
from app.services.scheduler_service import sync_source
from app import db

bp = Blueprint('main', __name__)

@bp.route('/')
def index():
    """Redirect to dashboard if authenticated, otherwise to login"""
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    return redirect(url_for('main.login'))

@bp.route('/login', methods=['GET', 'POST'])
def login():
    """Login page"""
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        remember = request.form.get('remember', False) == 'on'

        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            login_user(user, remember=remember)
            next_page = request.args.get('next')
            return redirect(next_page) if next_page else redirect(url_for('main.dashboard'))
        else:
            flash('Invalid username or password', 'danger')

    return render_template('login.html')

@bp.route('/dashboard')
@login_required
def dashboard():
    """Dashboard page - protected route with shopping stats"""
    # Get stats for dashboard
    total_sources = Source.query.filter_by(user_id=current_user.id).count()
    active_sources = Source.query.filter_by(user_id=current_user.id, status='active').count()

    total_products = Product.query.join(Source).filter(Source.user_id == current_user.id).count()
    favorite_products = Product.query.join(Source).filter(
        Source.user_id == current_user.id,
        Product.is_favorite == True
    ).count()

    # Price drops (products where current price < original price)
    price_drops = Product.query.join(Source).filter(
        Source.user_id == current_user.id,
        Product.original_price.isnot(None),
        Product.current_price < Product.original_price
    ).count()

    # Recent products (last 5)
    recent_products = Product.query.join(Source).filter(
        Source.user_id == current_user.id
    ).order_by(Product.first_seen_at.desc()).limit(5).all()

    # Recent price drops (last 5 products with price drops)
    recent_deals = Product.query.join(Source).filter(
        Source.user_id == current_user.id,
        Product.original_price.isnot(None),
        Product.current_price < Product.original_price
    ).order_by(Product.last_updated_at.desc()).limit(5).all()

    return render_template('dashboard.html',
                          total_sources=total_sources,
                          active_sources=active_sources,
                          total_products=total_products,
                          favorite_products=favorite_products,
                          price_drops=price_drops,
                          recent_products=recent_products,
                          recent_deals=recent_deals)

@bp.route('/logout')
@login_required
def logout():
    """Logout user"""
    logout_user()
    session.pop('chat_history', None)
    flash('You have been logged out successfully', 'success')
    return redirect(url_for('main.login'))


@bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    """Settings page for API configuration"""
    # Ensure user has settings record
    if not current_user.settings:
        current_user.settings = UserSettings(user_id=current_user.id)
        db.session.commit()

    if request.method == 'POST':
        api_key = request.form.get('api_key', '').strip()
        model = request.form.get('model', 'claude-3-5-haiku-latest')

        # Only update API key if it doesn't contain masked characters
        if api_key and '*' not in api_key:
            current_user.settings.set_api_key(api_key)

        current_user.settings.selected_model = model
        db.session.commit()
        flash('Settings saved successfully', 'success')
        return redirect(url_for('main.settings'))

    return render_template('settings.html',
                           masked_key=current_user.settings.get_masked_key(),
                           current_model=current_user.settings.selected_model,
                           models=AVAILABLE_MODELS)


@bp.route('/chat')
@login_required
def chat():
    """Chat page"""
    if not current_user.settings or not current_user.settings.get_api_key():
        flash('Please configure your API key in settings first', 'warning')
        return redirect(url_for('main.settings'))

    # Get model label for display
    model = current_user.settings.selected_model
    model_label = dict(AVAILABLE_MODELS).get(model, model)

    return render_template('chat.html', current_model_label=model_label)


@bp.route('/api/test-connection', methods=['POST'])
@login_required
def test_connection():
    """API endpoint to test Anthropic connection"""
    data = request.get_json()
    api_key = data.get('api_key', '')

    # If masked key, use stored key
    if '*' in api_key and current_user.settings:
        api_key = current_user.settings.get_api_key()

    if not api_key:
        return jsonify({'success': False, 'message': 'No API key provided'})

    service = AnthropicService(api_key)
    success, message = service.test_connection()
    return jsonify({'success': success, 'message': message})


@bp.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    """API endpoint for chat messages"""
    if not current_user.settings or not current_user.settings.get_api_key():
        return jsonify({'success': False, 'error': 'API key not configured'})

    data = request.get_json()
    user_message = data.get('message', '').strip()

    if not user_message:
        return jsonify({'success': False, 'error': 'Empty message'})

    # Get or initialize chat history
    if 'chat_history' not in session:
        session['chat_history'] = []

    # Add user message to history
    session['chat_history'].append({
        'role': 'user',
        'content': user_message
    })

    try:
        service = AnthropicService(current_user.settings.get_api_key())
        response = service.chat(
            messages=session['chat_history'],
            model=current_user.settings.selected_model
        )

        # Add assistant response to history
        session['chat_history'].append({
            'role': 'assistant',
            'content': response
        })
        session.modified = True

        return jsonify({'success': True, 'response': response})
    except Exception as e:
        # Remove the failed user message from history
        session['chat_history'].pop()
        session.modified = True
        return jsonify({'success': False, 'error': str(e)})


@bp.route('/api/chat/clear', methods=['POST'])
@login_required
def clear_chat():
    """Clear chat history"""
    session['chat_history'] = []
    return jsonify({'success': True})


# =============================================================================
# Sources Routes
# =============================================================================

@bp.route('/sources')
@login_required
def sources():
    """List all sources for current user"""
    user_sources = Source.query.filter_by(user_id=current_user.id).order_by(Source.created_at.desc()).all()
    return render_template('sources.html', sources=user_sources)


@bp.route('/sources/new', methods=['GET', 'POST'])
@login_required
def source_new():
    """Add a new source"""
    if not current_user.settings or not current_user.settings.get_api_key():
        flash('Please configure your API key in settings first', 'warning')
        return redirect(url_for('main.settings'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        url = request.form.get('url', '').strip()
        sync_interval = int(request.form.get('sync_interval', 24))
        selectors_json = request.form.get('selectors', '')

        if not name or not url:
            flash('Name and URL are required', 'danger')
            return render_template('source_new.html')

        # Parse selectors if provided
        selectors = None
        if selectors_json:
            import json
            try:
                selectors = json.loads(selectors_json)
            except json.JSONDecodeError:
                pass

        # Create the source
        source = Source(
            user_id=current_user.id,
            name=name,
            base_url=url,
            sync_interval_hours=sync_interval,
            selectors=selectors,
            selector_version=1 if selectors else 0,
            next_sync_at=datetime.utcnow()  # Sync immediately
        )
        db.session.add(source)
        db.session.commit()

        flash(f'Source "{name}" added successfully!', 'success')
        return redirect(url_for('main.sources'))

    return render_template('source_new.html')


@bp.route('/sources/<int:source_id>')
@login_required
def source_detail(source_id):
    """View source details"""
    source = Source.query.filter_by(id=source_id, user_id=current_user.id).first_or_404()
    products = Product.query.filter_by(source_id=source_id).order_by(Product.last_updated_at.desc()).limit(50).all()
    sync_logs = SyncLog.query.filter_by(source_id=source_id).order_by(SyncLog.started_at.desc()).limit(10).all()
    return render_template('source_detail.html', source=source, products=products, sync_logs=sync_logs)


@bp.route('/sources/<int:source_id>/edit', methods=['GET', 'POST'])
@login_required
def source_edit(source_id):
    """Edit a source"""
    source = Source.query.filter_by(id=source_id, user_id=current_user.id).first_or_404()

    if request.method == 'POST':
        source.name = request.form.get('name', source.name).strip()
        source.sync_interval_hours = int(request.form.get('sync_interval', source.sync_interval_hours))
        source.status = request.form.get('status', source.status)
        db.session.commit()
        flash('Source updated successfully', 'success')
        return redirect(url_for('main.source_detail', source_id=source_id))

    return render_template('source_edit.html', source=source)


@bp.route('/sources/<int:source_id>/delete', methods=['POST'])
@login_required
def source_delete(source_id):
    """Delete a source"""
    source = Source.query.filter_by(id=source_id, user_id=current_user.id).first_or_404()
    name = source.name
    db.session.delete(source)
    db.session.commit()
    flash(f'Source "{name}" deleted', 'success')
    return redirect(url_for('main.sources'))


@bp.route('/sources/<int:source_id>/sync', methods=['POST'])
@login_required
def source_sync(source_id):
    """Trigger manual sync for a source"""
    source = Source.query.filter_by(id=source_id, user_id=current_user.id).first_or_404()

    try:
        sync_log = sync_source(source)
        if sync_log.status == 'success':
            flash(f'Sync completed! Found {sync_log.products_found} products, '
                  f'added {sync_log.products_added}, updated {sync_log.products_updated}', 'success')
        else:
            flash(f'Sync failed: {sync_log.error_message}', 'danger')
    except Exception as e:
        flash(f'Sync error: {str(e)}', 'danger')

    return redirect(url_for('main.source_detail', source_id=source_id))


@bp.route('/api/analyze-url', methods=['POST'])
@login_required
def api_analyze_url():
    """Analyze a URL and return detected products preview with SSE streaming"""
    from flask import Response, stream_with_context
    import json as json_module

    if not current_user.settings or not current_user.settings.get_api_key():
        return jsonify({'success': False, 'error': 'API key not configured'})

    data = request.get_json()
    url = data.get('url', '').strip()

    if not url:
        return jsonify({'success': False, 'error': 'URL is required'})

    def generate():
        def send_progress(message, status='progress'):
            return f"data: {json_module.dumps({'type': status, 'message': message})}\n\n"

        try:
            yield send_progress(f"Starting analysis for {url}")

            # Import services
            from app.services.scraper_service import ScraperService
            from app.services.html_fetcher_service import HtmlFetcherService

            api_key = current_user.settings.get_api_key()
            model = current_user.settings.selected_model or 'claude-3-5-haiku-latest'

            # Step 1: Fetch HTML
            yield send_progress("Fetching page content...")
            fetcher = HtmlFetcherService()

            yield send_progress("Trying static HTTP request...")
            html, error = fetcher.fetch_static(url)

            if error and ('HTTP error' in error or 'CAPTCHA' in error):
                yield send_progress(f"Static fetch failed: {error}")
                yield send_progress("Launching headless browser (Playwright stealth mode)...")
                html, error = fetcher.fetch_with_javascript(url)

                if html:
                    yield send_progress(f"Browser fetch successful! Got {len(html):,} bytes")
                else:
                    yield send_progress(f"Browser fetch also failed: {error}", 'error')
                    yield f"data: {json_module.dumps({'type': 'error', 'error': error})}\n\n"
                    return
            elif html:
                yield send_progress(f"Static fetch successful! Got {len(html):,} bytes")

                # Check if JS might be needed
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, 'lxml')
                body_text = soup.get_text(strip=True)
                if len(body_text) < 500:
                    yield send_progress("Page content seems thin, trying browser...")
                    js_html, _ = fetcher.fetch_with_javascript(url)
                    if js_html and len(js_html) > len(html):
                        html = js_html
                        yield send_progress(f"Browser got more content: {len(html):,} bytes")
            else:
                yield send_progress(f"Fetch failed: {error}", 'error')
                yield f"data: {json_module.dumps({'type': 'error', 'error': error})}\n\n"
                return

            # Step 2: Clean HTML
            yield send_progress("Cleaning HTML (removing scripts, styles, navigation)...")
            cleaned_html = fetcher.clean_html_for_llm(html)
            yield send_progress(f"Cleaned HTML: {len(cleaned_html):,} chars")

            # Step 3: Analyze with LLM
            yield send_progress(f"Sending to Claude ({model}) for analysis...")
            yield send_progress("AI is analyzing page structure and generating selectors...")

            scraper = ScraperService(api_key=api_key, model=model)
            selectors, error = scraper.analyze_url_with_html(url, cleaned_html)

            if error:
                yield send_progress(f"Selector generation failed: {error}", 'error')
                yield send_progress("Falling back to direct LLM extraction...")

            # Step 4: Extract products
            yield send_progress("Extracting products using detected patterns...")

            if selectors and not selectors.get('use_llm_extraction'):
                products = scraper._extract_with_selectors(html, selectors, url)
                yield send_progress(f"Selector-based extraction found {len(products)} products")

                if not products:
                    yield send_progress("No products with selectors, trying direct LLM extraction...")
                    products, _ = scraper._extract_with_llm(cleaned_html, url)
            else:
                yield send_progress("Using direct LLM extraction...")
                products, _ = scraper._extract_with_llm(cleaned_html, url)

            yield send_progress(f"Found {len(products)} products!", 'success')
            yield send_progress(f"Used {scraper.get_tokens_used():,} API tokens")

            # Convert Decimal to float for JSON serialization
            products_serializable = []
            for p in products[:10]:  # Max 10 for preview
                product = dict(p)
                if product.get('price'):
                    product['price'] = float(product['price'])
                if product.get('original_price'):
                    product['original_price'] = float(product['original_price'])
                products_serializable.append(product)

            # Send final result
            yield f"data: {json_module.dumps({'type': 'complete', 'success': True, 'selectors': selectors, 'products': products_serializable, 'tokens_used': scraper.get_tokens_used()})}\n\n"

        except Exception as e:
            yield send_progress(f"Error: {str(e)}", 'error')
            yield f"data: {json_module.dumps({'type': 'error', 'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


# =============================================================================
# Products Routes
# =============================================================================

@bp.route('/products')
@login_required
def products():
    """List all products for current user"""
    # Get filter parameters
    filter_type = request.args.get('filter', 'all')
    source_id = request.args.get('source_id', type=int)
    sort_by = request.args.get('sort', 'updated')
    search = request.args.get('search', '').strip()

    # Base query - join with sources to filter by user
    query = Product.query.join(Source).filter(Source.user_id == current_user.id)

    # Apply filters
    if filter_type == 'favorites':
        query = query.filter(Product.is_favorite == True)
    elif filter_type == 'unavailable':
        query = query.filter(Product.is_available == False)
    elif filter_type == 'price_drops':
        # Products where current price < original price
        query = query.filter(
            Product.original_price.isnot(None),
            Product.current_price < Product.original_price
        )

    if source_id:
        query = query.filter(Product.source_id == source_id)

    if search:
        query = query.filter(Product.name.ilike(f'%{search}%'))

    # Apply sorting
    if sort_by == 'price_asc':
        query = query.order_by(Product.current_price.asc().nulls_last())
    elif sort_by == 'price_desc':
        query = query.order_by(Product.current_price.desc().nulls_last())
    elif sort_by == 'name':
        query = query.order_by(Product.name.asc())
    elif sort_by == 'added':
        query = query.order_by(Product.first_seen_at.desc())
    else:  # default: updated
        query = query.order_by(Product.last_updated_at.desc())

    products_list = query.limit(100).all()

    # Get user's sources for filter dropdown
    sources_list = Source.query.filter_by(user_id=current_user.id).all()

    return render_template('products.html',
                          products=products_list,
                          sources=sources_list,
                          current_filter=filter_type,
                          current_source_id=source_id,
                          current_sort=sort_by,
                          search=search)


@bp.route('/products/<int:product_id>')
@login_required
def product_detail(product_id):
    """View product details"""
    product = Product.query.join(Source).filter(
        Product.id == product_id,
        Source.user_id == current_user.id
    ).first_or_404()

    # Get price history for chart
    price_history = PriceHistory.query.filter_by(product_id=product_id).order_by(PriceHistory.recorded_at.asc()).all()

    return render_template('product_detail.html', product=product, price_history=price_history)


@bp.route('/products/<int:product_id>/favorite', methods=['POST'])
@login_required
def product_toggle_favorite(product_id):
    """Toggle favorite status for a product"""
    product = Product.query.join(Source).filter(
        Product.id == product_id,
        Source.user_id == current_user.id
    ).first_or_404()

    product.is_favorite = not product.is_favorite
    db.session.commit()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True, 'is_favorite': product.is_favorite})

    flash('Favorite status updated', 'success')
    return redirect(url_for('main.product_detail', product_id=product_id))


@bp.route('/products/<int:product_id>/notes', methods=['POST'])
@login_required
def product_update_notes(product_id):
    """Update product notes"""
    product = Product.query.join(Source).filter(
        Product.id == product_id,
        Source.user_id == current_user.id
    ).first_or_404()

    data = request.get_json()
    product.user_notes = data.get('notes', '')
    db.session.commit()

    return jsonify({'success': True})


@bp.route('/products/<int:product_id>/delete', methods=['POST'])
@login_required
def product_delete(product_id):
    """Delete a product"""
    product = Product.query.join(Source).filter(
        Product.id == product_id,
        Source.user_id == current_user.id
    ).first_or_404()

    db.session.delete(product)
    db.session.commit()

    flash('Product deleted', 'success')
    return redirect(url_for('main.products'))


@bp.route('/api/products/<int:product_id>/history')
@login_required
def api_product_history(product_id):
    """Get price history for a product (for charts)"""
    product = Product.query.join(Source).filter(
        Product.id == product_id,
        Source.user_id == current_user.id
    ).first_or_404()

    history = PriceHistory.query.filter_by(product_id=product_id).order_by(PriceHistory.recorded_at.asc()).all()

    return jsonify({
        'success': True,
        'history': [
            {
                'price': float(h.price),
                'recorded_at': h.recorded_at.isoformat()
            }
            for h in history
        ]
    })
