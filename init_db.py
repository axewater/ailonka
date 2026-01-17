#!/usr/bin/env python3
"""
Database initialization script
Creates tables and adds default admin user (admin/admin)
"""

from app import create_app, db
from app.models import User

def init_database():
    """Initialize database with tables and default user"""
    app = create_app()

    with app.app_context():
        print("Creating database tables...")
        db.create_all()
        print("✓ Tables created successfully")

        # Check if admin user already exists
        admin = User.query.filter_by(username='admin').first()
        if admin:
            print("⚠ Admin user already exists, skipping creation")
        else:
            # Create admin user
            admin = User(username='admin')
            admin.set_password('admin')
            db.session.add(admin)
            db.session.commit()
            print("✓ Admin user created (username: admin, password: admin)")

        print("\n✓ Database initialization complete!")
        print("You can now run the application with: python app.py")

if __name__ == '__main__':
    init_database()
