import os, tempfile
# One shared app per pytest process: set every variable the test modules need before the first app import.
for k, v in dict(DATABASE_URL=f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'all.db')}", APP_ENV="test", RUN_EXPIRY_LOOP="false", PAYMENT_PROVIDER="mock",
                 ADMIN_EMAIL="admin@shop.dev", ADMIN_PASSWORD="adminpass123", JWT_SECRET="test-secret", STRIPE_WEBHOOK_SECRET="whsec_test",
                 SHOPIFY_API_SECRET="shpss_test", SDR_OUTREACH_BREVO_INBOUND_SECRET="insec", SDR_OUTREACH_BREVO_WEBHOOK_SECRET="evsec").items():
    os.environ[k] = v
