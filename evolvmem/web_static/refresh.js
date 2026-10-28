(() => {
  'use strict';
  window.EvolvRefresh = {
    initNavigation(root) {
      const toggle = root.querySelector('[data-more-toggle]');
      const menu = root.querySelector('[data-more-menu]');
      if (!toggle || !menu) return;
      toggle.addEventListener('click', event => {
        event.preventDefault();
        menu.hidden = !menu.hidden;
        toggle.setAttribute('aria-expanded', String(!menu.hidden));
      });
      menu.querySelectorAll('a').forEach(link => {
        link.addEventListener('click', () => {
          menu.hidden = true;
          toggle.setAttribute('aria-expanded', 'false');
        });
      });
      document.addEventListener('click', event => {
        if (!event.target.closest('.refresh-more')) {
          menu.hidden = true;
          toggle.setAttribute('aria-expanded', 'false');
        }
      });
    },
    guard() {
      let latest = 0;
      return id => {
        latest = Math.max(latest, id);
        return id === latest;
      };
    },
    bindCardKeys(selector) {
      document.querySelectorAll(selector).forEach(card => {
        card.addEventListener('keydown', event => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            card.click();
          }
        });
      });
    }
  };
})();
