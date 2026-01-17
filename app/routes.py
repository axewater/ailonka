from flask import Blueprint, render_template, redirect, url_for, request, flash, jsonify, session
from flask_login import login_user, logout_user, login_required, current_user
from app.models import User, UserSettings
from app.services.anthropic_service import AnthropicService, AVAILABLE_MODELS
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
    """Dashboard page - protected route"""
    return render_template('dashboard.html')

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
