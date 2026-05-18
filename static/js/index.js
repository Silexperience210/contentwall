/**
 * ContentWall Admin Panel JavaScript (v1.1.0)
 *
 * Adds: stats, archive/edit, bundle uploads, CSV export, 30d chart.
 */

const blankFormData = () => ({
  id: null,
  title: '',
  description: '',
  content_type: 'article',
  article_content: '',
  image_file: null,
  bundle_files: [],
  amount: 100,
  currency: 'sat',
  memo: '',
  remembers: true,
  release_delay_seconds: 0,
  scheduled_at: '',
  onion_hostname: '',
  teaser_text: '',
  teaser_blur: true,
  access_duration_seconds: 0,
  webhook_url: '',
  max_views: 0,
});

window.app = Vue.createApp({
  data() {
    return {
      items: [],
      timeseries: [],
      chart: null,
      loading: false,
      showArchived: false,
      columns: [
        { name: 'title',        label: 'Title',     field: 'title',        align: 'left',   sortable: true },
        { name: 'content_type', label: 'Type',      field: 'content_type', align: 'center', sortable: true },
        { name: 'price',        label: 'Price',     align: 'center',       sortable: true },
        { name: 'payments',     label: 'Payments',  align: 'center'        },
        { name: 'status',       label: 'Flags',     align: 'center'        },
        { name: 'actions',      label: 'Actions',   align: 'right'         },
      ],
      itemDialog: { show: false, loading: false, data: blankFormData() },
      deleteDialog: { show: false, loading: false, item: {} },
      statsDialog: { show: false, title: '', data: { payment_count: 0, total_sats: 0, unique_payers: 0 } },
    };
  },

  computed: {
    isFormValid() {
      const d = this.itemDialog.data;
      if (!d.title || !d.amount || d.amount < 1) return false;
      if (!d.id) {
        if (d.content_type === 'article' && !d.article_content) return false;
        if (d.content_type === 'image' && !d.image_file) return false;
        if (d.content_type === 'bundle' && (!d.bundle_files || !d.bundle_files.length)) return false;
      }
      return true;
    },
    totalPayments() {
      return this.items.reduce((acc, i) => acc + (i.payment_count || 0), 0);
    },
    totalSats() {
      return this.items.reduce((acc, i) => acc + (i.total_sats || 0), 0);
    },
  },

  mounted() {
    this.loadAll();
  },

  methods: {
    async loadAll() {
      await this.loadItems();
      await this.loadTimeseries();
    },

    async loadItems() {
      this.loading = true;
      try {
        const resp = await LNbits.api.request(
          'GET',
          `/contentwall/api/v1/items?include_archived=${this.showArchived}`,
          g.user.wallets[0].inkey,
        );
        if (resp.data) this.items = resp.data;
      } catch (err) {
        LNbits.utils.notifyApiError(err);
      } finally {
        this.loading = false;
      }
    },

    async loadTimeseries() {
      try {
        const resp = await LNbits.api.request(
          'GET',
          '/contentwall/api/v1/stats/timeseries?days=30',
          g.user.wallets[0].inkey,
        );
        this.timeseries = resp.data || [];
        this.$nextTick(() => this.renderChart());
      } catch (err) {
        // Chart is optional
      }
    },

    renderChart() {
      const el = document.getElementById('cw-chart');
      if (!el || typeof Chart === 'undefined') return;
      if (this.chart) this.chart.destroy();

      const labels = this.timeseries.map(d => d.day.slice(5));
      const data = this.timeseries.map(d => d.total_sats);
      if (!labels.length) return;

      this.chart = new Chart(el, {
        type: 'bar',
        data: {
          labels,
          datasets: [{
            label: 'sats / day',
            data,
            backgroundColor: 'rgba(255, 107, 0, 0.7)',
            borderColor: 'rgba(255, 107, 0, 1)',
            borderWidth: 1,
          }],
        },
        options: {
          responsive: true,
          plugins: { legend: { display: false } },
          scales: {
            y: { beginAtZero: true, ticks: { precision: 0 } },
          },
        },
      });
    },

    contentTypeColor(t) {
      return { article: 'blue', image: 'purple', bundle: 'teal' }[t] || 'grey';
    },

    formatDuration(s) {
      if (s >= 86400) return Math.round(s / 86400) + 'd';
      if (s >= 3600)  return Math.round(s / 3600)  + 'h';
      if (s >= 60)    return Math.round(s / 60)    + 'm';
      return s + 's';
    },

    openCreateDialog() {
      this.itemDialog = { show: true, loading: false, data: blankFormData() };
    },

    openEditDialog(item) {
      this.itemDialog = {
        show: true,
        loading: false,
        data: {
          ...blankFormData(),
          id: item.id,
          title: item.title,
          description: item.description || '',
          content_type: item.content_type,
          amount: item.amount,
          currency: item.currency,
          memo: item.memo || '',
          remembers: !!item.remembers,
          release_delay_seconds: item.release_delay_seconds || 0,
          scheduled_at: item.scheduled_at || '',
          onion_hostname: item.onion_hostname || '',
          teaser_text: item.teaser_text || '',
          teaser_blur: item.teaser_blur === undefined ? true : !!item.teaser_blur,
          access_duration_seconds: item.access_duration_seconds || 0,
          webhook_url: item.webhook_url || '',
          max_views: item.max_views || 0,
        },
      };
    },

    onImageSelected(file) {
      this.itemDialog.data.image_file = file;
    },

    onBundleSelected(files) {
      this.itemDialog.data.bundle_files = Array.from(files || []);
    },

    async sendItemDialog() {
      this.itemDialog.loading = true;
      try {
        const d = this.itemDialog.data;
        const wallet = g.user.wallets[0];

        // EDIT mode: PATCH
        if (d.id) {
          const patch = {
            title: d.title,
            description: d.description,
            amount: d.amount,
            currency: d.currency,
            memo: d.memo,
            remembers: d.remembers,
            release_delay_seconds: parseInt(d.release_delay_seconds) || 0,
            scheduled_at: d.scheduled_at || null,
            onion_hostname: d.onion_hostname || null,
            teaser_text: d.teaser_text || null,
            teaser_blur: d.teaser_blur,
            access_duration_seconds: parseInt(d.access_duration_seconds) || 0,
            webhook_url: d.webhook_url || null,
            max_views: parseInt(d.max_views) || 0,
          };
          await LNbits.api.request(
            'PATCH',
            `/contentwall/api/v1/items/${d.id}`,
            wallet.adminkey,
            patch,
          );
          this.itemDialog.show = false;
          this.$q.notify({ type: 'positive', message: 'Item updated' });
          await this.loadAll();
          return;
        }

        // CREATE mode
        const createData = {
          title: d.title,
          description: d.description,
          content_type: d.content_type,
          article_content: d.content_type === 'article' ? d.article_content : null,
          amount: d.amount,
          currency: d.currency,
          memo: d.memo || `ContentWall: ${d.title}`,
          remembers: d.remembers,
          release_delay_seconds: parseInt(d.release_delay_seconds) || 0,
          scheduled_at: d.scheduled_at || null,
          onion_hostname: d.onion_hostname || null,
          teaser_text: d.teaser_text || null,
          teaser_blur: d.teaser_blur,
          access_duration_seconds: parseInt(d.access_duration_seconds) || 0,
          webhook_url: d.webhook_url || null,
          max_views: parseInt(d.max_views) || 0,
        };

        const resp = await LNbits.api.request(
          'POST',
          '/contentwall/api/v1/items',
          wallet.adminkey,
          createData,
        );
        if (!resp.data) throw new Error('Failed to create item');
        const itemId = resp.data.id;

        // Image upload
        if (d.content_type === 'image' && d.image_file) {
          const fd = new FormData();
          fd.append('upload_file', d.image_file);
          const r = await fetch(`/contentwall/api/v1/items/${itemId}/upload`, {
            method: 'POST',
            headers: { 'X-Api-Key': wallet.adminkey },
            body: fd,
          });
          if (!r.ok) {
            const err = await r.json();
            throw new Error(err.detail || 'Upload failed');
          }
        }

        // Bundle uploads (one POST per file)
        if (d.content_type === 'bundle' && d.bundle_files && d.bundle_files.length) {
          for (const file of d.bundle_files) {
            const fd = new FormData();
            fd.append('upload_file', file);
            const r = await fetch(`/contentwall/api/v1/items/${itemId}/files`, {
              method: 'POST',
              headers: { 'X-Api-Key': wallet.adminkey },
              body: fd,
            });
            if (!r.ok) {
              const err = await r.json();
              throw new Error(err.detail || `Upload failed for ${file.name}`);
            }
          }
        }

        this.itemDialog.show = false;
        this.$q.notify({ type: 'positive', message: 'Content item created!' });
        await this.loadAll();
      } catch (err) {
        LNbits.utils.notifyApiError(err);
      } finally {
        this.itemDialog.loading = false;
      }
    },

    openStats(item) {
      this.statsDialog = {
        show: true,
        title: item.title,
        data: {
          payment_count: item.payment_count || 0,
          total_sats: item.total_sats || 0,
          unique_payers: item.unique_payers || 0,
          last_payment_at: item.last_payment_at || null,
        },
      };
    },

    openDeleteDialog(item) {
      this.deleteDialog = { show: true, loading: false, item };
    },

    async archiveItem(item) {
      this.deleteDialog.loading = true;
      try {
        await LNbits.api.request(
          'POST',
          `/contentwall/api/v1/items/${item.id}/archive`,
          g.user.wallets[0].adminkey,
        );
        this.deleteDialog.show = false;
        this.$q.notify({ type: 'positive', message: 'Item archived' });
        await this.loadAll();
      } catch (err) {
        LNbits.utils.notifyApiError(err);
      } finally {
        this.deleteDialog.loading = false;
      }
    },

    async confirmDelete() {
      this.deleteDialog.loading = true;
      try {
        await LNbits.api.request(
          'DELETE',
          `/contentwall/api/v1/items/${this.deleteDialog.item.id}`,
          g.user.wallets[0].adminkey,
        );
        this.deleteDialog.show = false;
        this.$q.notify({ type: 'positive', message: 'Item deleted' });
        await this.loadAll();
      } catch (err) {
        LNbits.utils.notifyApiError(err);
      } finally {
        this.deleteDialog.loading = false;
      }
    },

    copyLink(item) {
      const url = `${window.location.origin}/contentwall/${item.id}`;
      navigator.clipboard.writeText(url).then(() => {
        this.$q.notify({ type: 'positive', message: 'Link copied' });
      });
    },

    openPublicPage(itemId) {
      window.open(`/contentwall/${itemId}`, '_blank');
    },

    async exportCsv() {
      try {
        const wallet = g.user.wallets[0];
        const r = await fetch('/contentwall/api/v1/stats/export.csv', {
          headers: { 'X-Api-Key': wallet.inkey },
        });
        if (!r.ok) throw new Error('Export failed');
        const blob = await r.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'contentwall-export.csv';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      } catch (err) {
        this.$q.notify({ type: 'negative', message: err.message });
      }
    },
  },
});

// Lazy-load Chart.js (admin only) — keeps the public page lightweight.
(function loadChartJs() {
  if (typeof Chart !== 'undefined') return;
  const s = document.createElement('script');
  s.src = 'https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js';
  document.head.appendChild(s);
})();

// IMPORTANT: do NOT call window.app.use(Quasar) or window.app.mount('#vue').
// LNbits' core init-app.js runs AFTER this script and does both itself.
