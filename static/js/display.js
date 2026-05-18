/**
 * ContentWall Public Display Page JavaScript (v1.2.0)
 *
 * Adds since v1.1.0:
 *   - Coupon application (with live discount preview)
 *   - Audio / video preview info
 *   - Purchase recording in localStorage for /me/purchases
 *   - Embed-mode aware (skips redirect, keeps content inline)
 */

window.app = Vue.createApp({
  data() {
    const item = itemData;
    return {
      itemId: item.id,
      paywallTitle: item.title,
      paywallDescription: item.description,
      paywallAmount: item.amount,
      paywallCurrency: item.currency,
      contentType: item.content_type,
      paywallMemo: item.memo,
      userAmount: item.amount,
      paymentReq: null,
      paymentHash: null,
      loading: false,
      paid: false,
      expired: false,
      contentUrl: null,
      onionUrl: null,
      remembers: true,
      ws: null,
      pollInterval: null,
      preview: null,
      lnurl: null,

      releaseDelaySeconds: item.release_delay_seconds || 0,
      scheduledAt: item.scheduled_at || null,
      contentUnlocked: true,
      unlockInSeconds: 0,
      delayProgress: 0,
      countdownInterval: null,

      accessDurationSeconds: item.access_duration_seconds || 0,
      maxViews: item.max_views || 0,
      viewsCount: 0,
      expiresAt: null,

      // v1.2.0
      couponInput: '',
      couponLoading: false,
      couponError: '',
      appliedCoupon: null,    // {code, original_amount, discounted_amount, savings}
      discountedAmount: null, // computed effective amount when coupon applied
    };
  },

  computed: {
    isScheduledPending() {
      if (!this.scheduledAt) return false;
      return new Date() < new Date(this.scheduledAt);
    },
    scheduledDateFormatted() {
      if (!this.scheduledAt) return '';
      return new Date(this.scheduledAt).toLocaleString();
    },
    expiresAtFormatted() {
      if (!this.expiresAt) return '';
      return new Date(this.expiresAt).toLocaleString();
    },
    formattedDuration() {
      const s = this.accessDurationSeconds;
      if (s >= 86400) return Math.round(s / 86400) + 'd';
      if (s >= 3600)  return Math.round(s / 3600)  + 'h';
      if (s >= 60)    return Math.round(s / 60)    + 'm';
      return s + 's';
    },
    effectiveAmount() {
      return this.discountedAmount !== null ? this.discountedAmount : this.paywallAmount;
    },
  },

  async mounted() {
    try {
      const r = await fetch(`/contentwall/api/v1/items/${this.itemId}/preview`);
      if (r.ok) this.preview = await r.json();
    } catch (_) {}

    try {
      const r2 = await fetch(`/contentwall/api/v1/lnurlp/${this.itemId}/encoded`);
      if (r2.ok) {
        const d = await r2.json();
        if (d.lnurl) this.lnurl = d.lnurl;
      }
    } catch (_) {}

    const urlParams = new URLSearchParams(window.location.search);
    const ph = urlParams.get('payment_hash');
    if (ph) {
      this.paymentHash = ph;
      this.checkPayment();
    }
  },

  beforeUnmount() { this.cleanup(); },

  methods: {
    cleanup() {
      if (this.ws) { this.ws.close(); this.ws = null; }
      if (this.pollInterval) { clearInterval(this.pollInterval); this.pollInterval = null; }
      if (this.countdownInterval) { clearInterval(this.countdownInterval); this.countdownInterval = null; }
    },

    async applyCoupon() {
      this.couponError = '';
      const code = (this.couponInput || '').trim().toUpperCase();
      if (!code) return;
      this.couponLoading = true;
      try {
        const r = await fetch(`/contentwall/api/v1/items/${this.itemId}/coupons/check/${encodeURIComponent(code)}`);
        if (!r.ok) {
          this.couponError = 'Could not verify coupon';
          return;
        }
        const d = await r.json();
        if (!d.valid) {
          this.couponError = d.reason || 'Invalid code';
          this.discountedAmount = null;
          this.appliedCoupon = null;
          return;
        }
        this.discountedAmount = d.discounted_amount;
        this.appliedCoupon = { code, ...d };
        if (this.userAmount < d.discounted_amount) this.userAmount = d.discounted_amount;
        this.couponError = '';
      } catch (err) {
        this.couponError = 'Network error';
      } finally {
        this.couponLoading = false;
      }
    },

    clearCoupon() {
      this.discountedAmount = null;
      this.appliedCoupon = null;
      this.couponInput = '';
      this.couponError = '';
      if (this.userAmount < this.paywallAmount) this.userAmount = this.paywallAmount;
    },

    async createInvoice() {
      if (this.isScheduledPending) {
        this.$q.notify({ type: 'warning', message: 'Not yet available' });
        return;
      }
      this.loading = true;
      try {
        const body = { amount: this.userAmount };
        if (this.appliedCoupon) body.coupon_code = this.appliedCoupon.code;

        const resp = await fetch(
          `/contentwall/api/v1/items/invoice/${this.itemId}`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          },
        );
        if (!resp.ok) {
          const err = await resp.json();
          throw new Error(err.detail || 'Failed to create invoice');
        }
        const data = await resp.json();
        this.paymentReq = data.payment_request;
        this.paymentHash = data.payment_hash;
        this.listenForPayment();
      } catch (err) {
        this.$q.notify({ type: 'negative', message: err.message || 'Failed to create invoice' });
      } finally {
        this.loading = false;
      }
    },

    listenForPayment() {
      const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const wsUrl = `${wsProtocol}//${window.location.host}/contentwall/api/v1/items/ws/${this.itemId}/${this.paymentHash}`;
      this.ws = new WebSocket(wsUrl);

      this.ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.paid) this.onPaymentSuccess();
      };
      this.ws.onerror = () => {
        this.pollInterval = setInterval(() => this.checkPayment(), 4000);
      };
    },

    recordPurchaseLocally() {
      try {
        const key = 'contentwall.purchases';
        const arr = JSON.parse(localStorage.getItem(key) || '[]');
        if (!arr.find(p => p.payment_hash === this.paymentHash)) {
          arr.push({
            payment_hash: this.paymentHash,
            item_id: this.itemId,
            title: this.paywallTitle,
            saved_at: new Date().toISOString(),
          });
          localStorage.setItem(key, JSON.stringify(arr));
        }
      } catch (_) {
        // localStorage may be disabled (private mode) — non-fatal.
      }
    },

    async checkPayment() {
      if (!this.paymentHash) return;
      try {
        const resp = await fetch(
          `/contentwall/api/v1/items/check/${this.itemId}`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ payment_hash: this.paymentHash }),
          },
        );
        if (!resp.ok) return;
        const data = await resp.json();
        if (data.paid) {
          this.paid = true;
          this.expired = !!data.expired;
          this.remembers = data.remembers;
          this.contentUrl = data.url;
          this.onionUrl = data.onion_url || null;
          this.releaseDelaySeconds = data.release_delay_seconds || 0;
          this.contentUnlocked = data.content_unlocked !== false;
          this.unlockInSeconds = data.unlock_in_seconds || 0;
          this.expiresAt = data.expires_at || null;
          this.viewsCount = data.views_count || 0;
          this.maxViews = data.max_views || this.maxViews;

          this.cleanup();
          this.recordPurchaseLocally();

          if (!this.expired && !this.contentUnlocked && this.unlockInSeconds > 0) {
            this.startCountdown();
          } else if (!this.expired && !EMBED_MODE) {
            // Embed mode: stay on the embedded card; let user click through
            setTimeout(() => { if (this.contentUrl) window.location.href = this.contentUrl; }, 1500);
          }
        }
      } catch (err) {
        console.error('Payment check error:', err);
      }
    },

    onPaymentSuccess() {
      this.paid = true;
      this.checkPayment();
    },

    startCountdown() {
      this.delayProgress = 0;
      const total = this.releaseDelaySeconds;
      let remaining = this.unlockInSeconds;
      this.countdownInterval = setInterval(() => {
        remaining -= 1;
        this.unlockInSeconds = Math.max(0, remaining);
        this.delayProgress = 1 - (remaining / total);
        if (remaining <= 0) {
          clearInterval(this.countdownInterval);
          this.contentUnlocked = true;
          this.delayProgress = 1;
          if (this.contentUrl && !EMBED_MODE) window.location.href = this.contentUrl;
        }
      }, 1000);
    },

    copyInvoice() {
      navigator.clipboard.writeText(this.paymentReq).then(() => {
        this.$q.notify({ type: 'positive', message: 'Invoice copied' });
      });
    },
    copyLnurl() {
      if (!this.lnurl) return;
      navigator.clipboard.writeText('lightning:' + this.lnurl).then(() => {
        this.$q.notify({ type: 'positive', message: 'LNURL copied — paste in any Lightning wallet' });
      });
    },
    cancelPayment() {
      this.paymentReq = null;
      this.cleanup();
    },
  },
});

// IMPORTANT: do NOT call window.app.use(Quasar) or window.app.mount('#vue').
