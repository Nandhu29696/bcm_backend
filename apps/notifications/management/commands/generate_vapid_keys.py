"""Print a VAPID key pair for Web Push. Paste both into .env; never commit them."""

import base64

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Generate VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY for browser push notifications."

    def handle(self, *args, **options):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        private = ec.generate_private_key(ec.SECP256R1())
        raw_private = private.private_numbers().private_value.to_bytes(32, "big")
        raw_public = private.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
        self.stdout.write(f"VAPID_PUBLIC_KEY={b64(raw_public)}")
        self.stdout.write(f"VAPID_PRIVATE_KEY={b64(raw_private)}")
