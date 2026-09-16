"""
WSGI config for bcm_backend.

Exposes the WSGI callable as a module-level variable named ``application``.
Defaults to production settings — deployment targets should not depend on an
environment variable being remembered.
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bcm_backend.settings.production")

application = get_wsgi_application()
