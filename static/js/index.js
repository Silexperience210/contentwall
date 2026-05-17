/**
 * ContentWall Admin Panel JavaScript
 */

window.app = Vue.createApp({
  data() {
    return {
      items: [],
      loading: false,
      columns: [
        { name: 'title', label: 'Title', field: 'title', align: 'left', sortable: true },
        { name: 'content_type', label: 'Type', field: 'content_type', align: 'center', sortable: true },
        { name: 'price', label: 'Price', align: 'center', sortable: true },
        { name: 'status', label: 'Status', align: 'center' },
        { name: 'actions', label: 'Actions', align: 'right' },
      ],
      itemDialog: {
        show: false,
        loading: false,
        data: {
          id: null,
          title: '',
          description: '',
          content_type: 'article',
          article_content: '',
          image_file: null,
          amount: 100,
          currency: 'sat',
          memo: '',
          remembers: true,
          release_delay_seconds: 0,
          scheduled_at: '',
          onion_hostname: '',
        },
      },
      deleteDialog: {
        show: false,
        loading: false,
        item: {},
      },
    };
  },

  computed: {
    isFormValid() {
      const d = this.itemDialog.data;
      if (!d.title || !d.amount || d.amount < 1) return false;
      if (!d.id && d.content_type === 'article' && !d.article_content) return false;
      if (!d.id && d.content_type === 'image' && !d.image_file) return false;
      return true;
    },
  },

  mounted() {
    this.loadItems();
  },

  methods: {
    async loadItems() {
      this.loading = true;
      try {
        const resp = await LNbits.api.request(
          'GET',
          '/contentwall/api/v1/items',
          g.user.wallets[0].inkey,
        );
        if (resp.data) {
          this.items = resp.data;
        }
      } catch (err) {
        LNbits.utils.notifyApiError(err);
      } finally {
        this.loading = false;
      }
    },

    openCreateDialog() {
      this.itemDialog = {
        show: true,
        loading: false,
        data: {
          id: null,
          title: '',
          description: '',
          content_type: 'article',
          article_content: '',
          image_file: null,
          amount: 100,
          currency: 'sat',
          memo: '',
          remembers: true,
          release_delay_seconds: 0,
          scheduled_at: '',
          onion_hostname: '',
        },
      };
    },

    onImageSelected(file) {
      this.itemDialog.data.image_file = file;
    },

    async sendItemDialog() {
      this.itemDialog.loading = true;
      try {
        const d = this.itemDialog.data;
        const wallet = g.user.wallets[0];

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
        };

        const resp = await LNbits.api.request(
          'POST',
          '/contentwall/api/v1/items',
          wallet.adminkey,
          createData,
        );

        if (!resp.data) {
          throw new Error('Failed to create item');
        }

        const itemId = resp.data.id;

        if (d.content_type === 'image' && d.image_file) {
          const formData = new FormData();
          formData.append('upload_file', d.image_file);

          const uploadResp = await fetch(
            `/contentwall/api/v1/items/${itemId}/upload`,
            {
              method: 'POST',
              headers: {
                'X-Api-Key': wallet.adminkey,
              },
              body: formData,
            },
          );

          if (!uploadResp.ok) {
            const err = await uploadResp.json();
            throw new Error(err.detail || 'Upload failed');
          }
        }

        this.itemDialog.show = false;
        this.$q.notify({
          type: 'positive',
          message: 'Content item created!',
        });
        this.loadItems();
      } catch (err) {
        LNbits.utils.notifyApiError(err);
      } finally {
        this.itemDialog.loading = false;
      }
    },

    openDeleteDialog(item) {
      this.deleteDialog = {
        show: true,
        loading: false,
        item: item,
      };
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
        this.$q.notify({
          type: 'positive',
          message: 'Item deleted',
        });
        this.loadItems();
      } catch (err) {
        LNbits.utils.notifyApiError(err);
      } finally {
        this.deleteDialog.loading = false;
      }
    },

    copyLink(item) {
      const url = item.public_url || `${window.location.origin}/contentwall/${item.id}`;
      navigator.clipboard.writeText(url).then(() => {
        this.$q.notify({ type: 'positive', message: 'Clearnet link copied!' });
      });
    },

    copyOnionLink(item) {
      const url = item.onion_url;
      if (!url) return;
      navigator.clipboard.writeText(url).then(() => {
        this.$q.notify({ type: 'positive', message: 'Onion link copied!' });
      });
    },

    openPublicPage(itemId) {
      window.open(`/contentwall/${itemId}`, '_blank');
    },
  },
});

// IMPORTANT: do NOT call window.app.use(Quasar) or window.app.mount('#vue').
// LNbits' core init-app.js runs AFTER this script and does both itself, along
// with adding the g/api/utils mixin. Mounting here breaks Quasar registration
// and produces a blank page.
