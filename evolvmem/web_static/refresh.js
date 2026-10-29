(() => {
  'use strict';
  window.EvolvRefresh = {
    initNavigation(root) {
      const toggle = root.querySelector('[data-more-toggle]');
      const menu = root.querySelector('[data-more-menu]');
      if (!toggle || !menu) return;
      const close = () => {
        menu.hidden = true;
        toggle.setAttribute('aria-expanded', 'false');
      };
      const place = () => {
        const anchor = toggle.getBoundingClientRect();
        const rect = menu.getBoundingClientRect();
        menu.style.left = Math.max(8,Math.min(innerWidth - rect.width - 8,anchor.right - rect.width)) + 'px';
        menu.style.top = Math.max(8,Math.min(innerHeight - rect.height - 8,anchor.bottom + 7)) + 'px';
      };
      // A fixed menu must live outside the horizontally scrollable navigation,
      // otherwise the header clips it even though its links remain focusable.
      document.body.append(menu);
      toggle.addEventListener('click', event => {
        event.preventDefault();
        if (!menu.hidden) { close(); return; }
        menu.hidden = false;
        toggle.setAttribute('aria-expanded', 'true');
        place();
      });
      menu.querySelectorAll('a').forEach(link => link.addEventListener('click', close));
      document.addEventListener('click', event => {
        if (!event.target.closest('.refresh-more') && !menu.contains(event.target)) close();
      });
      document.addEventListener('keydown', event => {
        if (event.key === 'Escape' && !menu.hidden) { close(); toggle.focus(); }
      });
      document.addEventListener('scroll', () => { if (!menu.hidden) place(); }, true);
      window.addEventListener('resize', () => { if (!menu.hidden) place(); });
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
