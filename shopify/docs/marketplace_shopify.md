# 8. Marketplace and Shopify Integration

8.1 Business Objective
The Marketplace module turns the store backend into a multi-seller marketplace and lets sellers bring their catalog from Shopify instead of re-keying it. The SDR platform is the acquisition channel: it finds and engages merchants, and the Meeting & CRM handoff ends with the merchant applying as a seller.

8.2 Seller Acquisition Flow
ICP (target merchants) → Prospect Discovery → Enrichment → Prospect Intelligence → Qualification → Outreach → Conversation → Meeting & CRM → Seller application → Admin approval → Shopify connection → Catalog import → Live storefront
Note: the SDR-to-store handoff is a documented process today. The merchant applies through the store ("Sell on the store" page or POST /sellers). There is no automated bridge from a CRM record to a seller account yet.

8.3 Seller Lifecycle
	•	pending: seller applied, cannot connect Shopify or import
	•	approved: admin approved, storefront is public, Shopify import allowed
	•	suspended: storefront hidden, import blocked

8.4 Shopify Connector Responsibilities
	•	Connect a store by *.myshopify.com domain and Admin API token (other hosts are rejected)
	•	Import products and variants (paginated), mapping price to integer minor units
	•	Re-import updates existing products instead of duplicating them
	•	Receive signed webhooks: products/create, products/update, inventory_levels/update
	•	Push stock back to Shopify after an order is paid (needs a location ID)
	•	Deduplicate webhook retries using the shared webhook event table

8.5 API Endpoints
POST /sellers
GET  /sellers/me
GET  /sellers/{slug}/products
PUT  /sellers/me/shopify
POST /sellers/me/shopify/import
POST /webhooks/shopify
GET  /admin/sellers
POST /admin/sellers/{id}/status?status=approved|suspended

8.6 Database Components
	•	sellers: owner user, name, slug, status, commission_pct
	•	product_sellers: product to seller ownership
	•	shopify_connections: seller, shop domain, access token, location ID, last import time
	•	shopify_links: local variant to Shopify product, variant and inventory item IDs

8.7 Configuration
SHOPIFY_API_SECRET=app-secret-used-to-verify-webhooks
SHOPIFY_API_VERSION=2025-07

8.8 Security Notes
	•	Webhook HMAC-SHA256 signature is verified on the raw body
	•	Access tokens are stored unencrypted today and must be encrypted before production
	•	Shopify pagination links are followed only if they stay on the connected shop host

8.9 Workflow Integration
Seller onboarding runs after Meeting & CRM and is independent of SDR Analytics. UI: "Sell on the store" page for sellers and a "Sellers" tab in Admin for approvals.

8.10 Current Completion Status
Status: Functional Prototype
Implemented
✓ Seller application, approval and suspension
✓ Seller public storefront
✓ Shopify connection, product import and re-import
✓ Signed Shopify webhooks with de-duplication
✓ Two-way stock sync (needs location ID)
✓ Streamlit seller page and admin Sellers tab
Pending
◦ Seller-created products without Shopify
◦ Per-seller order splitting, commission calculation and payouts
◦ Shopify OAuth install flow
◦ products/delete webhook and multi-location inventory
◦ Encrypted token storage
◦ Automated SDR-to-seller invitation from CRM

