# ContentWall - LNbits Extension

A real paywall extension for LNbits that stores articles and images server-side, serving them only after Lightning payment verification.

## Problem with Existing Paywall

The default LNbits Paywall extension redirects users to an external URL after payment. This means:
- The original URL can be shared freely, bypassing the paywall
- Content must be hosted elsewhere
- No true protection for the content

## ContentWall Solution

- **Stores content on the LNbits server** - articles as text, images as files
- **Serves content directly** after payment verification
- **No external URLs** that can be shared to bypass payment
- **Every access is verified** server-side using the payment hash
- Supports **articles** (text content) and **images** (file uploads)

## Features

- Create article-type or image-type paywall items
- Set price in sats or fiat (USD, EUR)
- Upload images (JPEG, PNG, GIF, WebP)
- Write articles with full text editor
- Optional "remember payments" for returning visitors
- Real-time WebSocket payment notifications
- Content is served inline after payment - no redirects
- QR code scanning for mobile payment

## Install

### Method 1: From GitHub (recommended)

1. In your LNbits instance, go to **Manage Server > Extensions**
2. Under **Extension Sources**, add:
   ```
   https://raw.githubusercontent.com/Silexperience210/contentwall/main/manifest.json
   ```
3. Go to **Extensions > All** tab and install **ContentWall**

### Method 2: Manual

1. Clone this repo into your LNbits extensions directory:
   ```bash
   cd lnbits/lnbits/extensions
   ln -s /path/to/contentwall contentwall
   ```
2. Restart LNbits

## Usage

1. Open the ContentWall extension in your LNbits wallet
2. Click **"New Content"**
3. Choose content type (Article or Image)
4. Fill in title, description, and content
5. Set price and currency
6. Click **Create**
7. Copy the public link and share it!

## Architecture

```
contentwall/
├── __init__.py          # Extension registration
├── models.py            # Pydantic data models
├── crud.py              # Database operations
├── migrations.py        # DB migrations
├── views.py             # Frontend routes
├── views_api.py         # API endpoints
├── tasks.py             # Background invoice listener
├── config.json          # Extension config
├── manifest.json        # Extension manifest
├── static/contentwall/js/
│   ├── index.js         # Admin panel logic
│   └── display.js       # Public payment page
└── templates/contentwall/
    ├── index.html       # Admin UI
    ├── display.html     # Payment page
    └── content.html     # Content viewer
```

## License

MIT
