# ContentWall - Real Paywall Extension

ContentWall is a LNbits extension that creates **real paywalls** with **server-hosted content**. Unlike the default Paywall extension that simply redirects to an external URL after payment, ContentWall stores your articles and images directly on the LNbits server and only serves them after payment verification.

## Key Differences from Paywall Extension

| Feature | Paywall | ContentWall |
|---------|---------|-------------|
| Content storage | External URL | Server-hosted |
| URL sharing risk | High (original URL can be shared) | None (content served by extension) |
| Content types | Links only | Articles + Images |
| Article hosting | No | Yes (stored server-side) |
| Image hosting | No | Yes (stored server-side) |
| Payment memory | Yes | Yes (optional) |

## How It Works

1. **Create content** in the admin panel (write an article or upload an image)
2. **Set a price** in sats (or fiat)
3. **Share the public link** with your audience
4. Visitors **pay via Lightning** to unlock the content
5. Content is **served directly** by the extension - no external URLs to leak

## Security Model

- Content is stored on the LNbits server, not on external servers
- Every content access requires payment verification
- Even if someone shares the link with `payment_hash`, the payment is verified server-side each time
- Optional "remember payments" feature allows returning visitors to keep access

## API Endpoints

### Admin Endpoints (require API key)
- `GET /api/v1/items` - List your content items
- `POST /api/v1/items` - Create a new content item
- `POST /api/v1/items/{id}/upload` - Upload image file
- `DELETE /api/v1/items/{id}` - Delete a content item

### Public Endpoints
- `GET /{item_id}` - Public payment page
- `POST /api/v1/items/invoice/{item_id}` - Create payment invoice
- `POST /api/v1/items/check/{item_id}` - Check payment status
- `GET /content/{item_id}?payment_hash=...` - View content (after payment)

## Installation

1. Install from GitHub repository in your LNbits instance
2. The extension will create its database tables automatically
3. Uploaded files are stored in `data/contentwall/files/`
